"""Web pages from the playground, shared by link: POST /chat/shares, DELETE /chat/shares/{token},
and GET /p/{token}, which anyone with the link can open without signing in.

A model wrote the page, so it is treated as something a stranger wrote. It is only ever served with a
sandbox Content-Security-Policy: the browser runs it as an opaque origin of its own, so its scripts
cannot read this site's cookies or storage or call the gateway as the person viewing it, and it may
not be framed by anyone. The token is the only key: unguessable, and the link ends when it is deleted.
"""
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, authenticate
from app.db import get_session
from app.models import SharedPage

router = APIRouter(tags=["shares"])

MAX_PAGE_BYTES = 2 * 1024 * 1024
# Its own scripts, dialogs, links that open tabs and forms that go nowhere; never this origin
PAGE_HEADERS = {
    "Content-Security-Policy": "sandbox allow-scripts allow-modals allow-popups allow-forms; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}
GONE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Page not found</title><body style="font:16px system-ui,sans-serif;margin:3rem auto;max-width:30rem;padding:0 1rem;color:#333">
<h1 style="font-size:1.3rem">This shared page is not here</h1>
<p>The link may be mistyped, or whoever shared the page has stopped sharing it.</p>"""


class ShareIn(BaseModel):
    html: str = Field(min_length=1, max_length=MAX_PAGE_BYTES)
    title: str = Field(default="", max_length=200)


def _shown(page, existing):
    return {"token": page.token, "path": f"/p/{page.token}", "title": page.title,
            "created_at": page.created_at.isoformat() if page.created_at else None, "existing": existing}


@router.post("/chat/shares")
async def share(payload: ShareIn, principal: Principal = Depends(authenticate),
                session: AsyncSession = Depends(get_session)):
    """A link to this page. The same page shared again by the same person gets the same link."""
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sign in to share a page")
    digest = hashlib.sha256(payload.html.encode()).hexdigest()
    page = (await session.execute(select(SharedPage).where(
        SharedPage.user_id == principal.user.id, SharedPage.sha256 == digest))).scalars().first()
    if page:
        return _shown(page, True)
    page = SharedPage(token=secrets.token_urlsafe(18), user_id=principal.user.id,
                      title=payload.title.strip()[:200], html=payload.html, sha256=digest)
    session.add(page)
    await session.commit()
    return _shown(page, False)


@router.delete("/chat/shares/{token}")
async def stop_sharing(token: str, principal: Principal = Depends(authenticate),
                       session: AsyncSession = Depends(get_session)):
    page = (await session.execute(select(SharedPage).where(SharedPage.token == token))).scalars().first()
    if page is None or not (principal.is_admin or (principal.user is not None and page.user_id == principal.user.id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such shared page")
    await session.delete(page)
    await session.commit()
    return {"deleted": token}


@router.get("/p/{token}", include_in_schema=False)
async def shared_page(token: str, session: AsyncSession = Depends(get_session)):
    page = None
    if 16 <= len(token) <= 64:
        page = (await session.execute(select(SharedPage).where(SharedPage.token == token))).scalars().first()
    if page is None:
        return HTMLResponse(GONE, status_code=404, headers=PAGE_HEADERS)
    page.views = (page.views or 0) + 1
    await session.commit()
    return HTMLResponse(page.html, headers=PAGE_HEADERS)
