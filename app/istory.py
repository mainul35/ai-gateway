"""Sends finished video stories to the person's istory studio, as private drafts.

istory is the story video platform; both it and the gateway sign people in through vsd-auth-server.
When a video story is saved here for someone who signed in that way, it goes to istory's ingest API
in the background. Nothing is published there: the draft waits in the person's istory studio until
they publish it themselves.

The gateway calls istory as itself, with a client-credentials token from vsd-auth-server whose
audience is istory's API (RFC 8707 `resource`) and which carries the istory:ingest scope; who the
video is for travels as the person's VSD `sub`. istory accepts it only from the client ids it lists,
and only for someone who has an istory account and has not turned imports off.

    istory.enabled=true
    istory.url=https://istory.example.com              # where istory's backend answers /api/ingest
    istory.client.id=ai-gateway-backend                # BACKEND_SERVICE client at vsd-auth-server
    istory.client.secret=...                           # or ISTORY_CLIENT_SECRET
    istory.token.url=                                  # defaults to sso.token.url

Every send is recorded in istory_exports. The file id is istory's externalId, so a retry never makes
a second draft. A send that is still running when the gateway stops is picked up again at startup.
"""
import asyncio
import json
import logging
import time

import httpx
from sqlalchemy import select

from app import settings
from app.db import session_factory
from app.models import ChatFile, IstoryExport, User, utcnow

log = logging.getLogger("istory")

# Seconds to wait before each retry of a send that failed for a reason that may pass
RETRY_DELAYS = (10, 60, 300, 900)

_token = {"value": None, "expires": 0.0}
_token_lock = asyncio.Lock()
# Running sends, kept so they are not garbage-collected half way through
_running = set()


class Permanent(Exception):
    """istory answered, and asking again will not change the answer."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def subject_of(user):
    """(sub, user_id) at vsd-auth-server, or (None, None) for an account that does not sign in there."""
    if not user.sso_subject:
        return None, None
    try:
        claims = json.loads(user.claims or "{}")
    except ValueError:
        claims = {}
    sub = claims.get(settings.sso_claim_id() or "sub") or user.sso_subject.split("|", 1)[-1]
    vsd_user_id = claims.get("user_id")
    return sub, (str(vsd_user_id) if vsd_user_id is not None else None)


def send_soon(user_id, file_id, title, language=None):
    """Starts sending one video in the background; returns at once. Does nothing while switched off."""
    if not settings.istory_enabled():
        return
    task = asyncio.create_task(_send(user_id, file_id, title, language))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def resume():
    """At startup: sends that were still running when the gateway stopped."""
    if not settings.istory_enabled():
        return
    async with session_factory()() as db:
        rows = (await db.execute(select(IstoryExport, ChatFile.prompt, ChatFile.name)
                                 .join(ChatFile, ChatFile.id == IstoryExport.file_id)
                                 .where(IstoryExport.status == "sending"))).all()
    for export, prompt, name in rows:
        send_soon(export.user_id, export.file_id, prompt or name or "Video story")


async def _send(user_id, file_id, title, language):
    try:
        async with session_factory()() as db:
            user = await db.get(User, user_id)
            sub, vsd_user_id = subject_of(user) if user else (None, None)
            if sub is None:
                return                      # a local account: there is nobody to send it to at istory
            export = (await db.execute(select(IstoryExport).where(IstoryExport.file_id == file_id))).scalar_one_or_none()
            if export is None:
                export = IstoryExport(user_id=user_id, file_id=file_id)
                db.add(export)
            elif export.status != "sending":
                return                      # already settled
            await db.commit()

        for attempt, delay in enumerate((0,) + RETRY_DELAYS):
            await asyncio.sleep(delay)
            try:
                async with session_factory()() as db:
                    stored = await db.get(ChatFile, file_id)
                    if stored is None:
                        raise Permanent(404, "the video was deleted before it could be sent")
                    data, name = stored.data, stored.name or f"story-{file_id}.mp4"
                result = await _post(data, name, file_id, sub, vsd_user_id, title, language)
            except Permanent as e:
                status = {"user_not_linked": "not_linked", "auto_import_disabled": "disabled"}.get(str(e), "failed")
                await _settle(file_id, status, attempt + 1, error=str(e))
                log.info("Video file %s not sent to istory: %s", file_id, e)
                return
            except Exception as e:          # network, 5xx, token trouble: worth another try
                await _settle(file_id, "sending", attempt + 1, error=str(e)[:500])
                log.warning("Sending video file %s to istory failed (attempt %d): %s", file_id, attempt + 1, e)
                continue
            await _settle(file_id, "sent", attempt + 1, video_id=result.get("videoId"),
                          studio_url=result.get("studioUrl"))
            log.info("Video file %s sent to istory as %s", file_id, result.get("videoId"))
            return
        await _settle(file_id, "failed", len(RETRY_DELAYS) + 1)
    except Exception:
        log.exception("Sending video file %s to istory broke", file_id)


async def _post(data, name, file_id, sub, vsd_user_id, title, language):
    """One send to istory's ingest API: its answer, Permanent when retrying is pointless, or an error."""
    fields ={"subject": sub, "externalId": str(file_id), "title": (title or "Video story")[:200],
              "tags": ",".join(t for t in ("ai-gateway", "story", language) if t)}
    if vsd_user_id:
        fields["userId"] = vsd_user_id
    if language:
        fields["language"] = language
    for fresh in (False, True):
        token = await _access_token(fresh)
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20)) as client:
            response = await client.post(f"{settings.istory_url()}/api/ingest/videos", data=fields,
                                         files={"file": (name, data, "video/mp4")},
                                         headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401 and not fresh:
            continue                        # the cached token was refused: get a new one, once
        break
    if response.status_code in (200, 202):
        return response.json()
    if response.status_code >= 500 or response.status_code in (401, 408, 429):
        raise RuntimeError(f"istory answered {response.status_code}: {response.text[:200]}")
    try:
        code = response.json().get("error") or response.text[:200]
    except ValueError:
        code = response.text[:200]
    raise Permanent(response.status_code, code)


async def _access_token(fresh=False):
    """A client-credentials token for istory's API, reused until a minute before it expires."""
    async with _token_lock:
        if not fresh and _token["value"] and time.monotonic() < _token["expires"] - 60:
            return _token["value"]
        data = {"grant_type": "client_credentials", "scope": settings.istory_scope(),
                "resource": settings.istory_resource()}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(settings.istory_token_url(), data=data,
                                         auth=(settings.istory_client_id(), settings.istory_client_secret() or ""),
                                         headers={"Accept": "application/json"})
        if response.status_code >= 400:
            raise RuntimeError(f"Token endpoint returned {response.status_code}: {response.text[:200]}")
        body = response.json()
        _token["value"] = body["access_token"]
        _token["expires"] = time.monotonic() + float(body.get("expires_in", 300))
        return _token["value"]


async def _settle(file_id, status, attempts, error=None, video_id=None, studio_url=None):
    async with session_factory()() as db:
        export = (await db.execute(select(IstoryExport).where(IstoryExport.file_id == file_id))).scalar_one_or_none()
        if export is None:
            return
        export.status = status
        export.attempts = attempts
        export.error = error
        if video_id:
            export.istory_video_id = video_id
            export.studio_url = studio_url
        export.updated_at = utcnow()
        await db.commit()


async def status_of(user_id, file_id):
    """What happened to one video's send, for the person who made it; None when it was never sent."""
    async with session_factory()() as db:
        export = (await db.execute(select(IstoryExport).where(IstoryExport.file_id == file_id,
                                                              IstoryExport.user_id == user_id))).scalar_one_or_none()
    if export is None:
        return None
    return {"status": export.status, "videoId": export.istory_video_id, "studioUrl": export.studio_url,
            "attempts": export.attempts, "error": export.error}
