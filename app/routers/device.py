"""A folder on the person's computer, connected to one conversation for the agent to read (device_files).

The program the connect command starts (bin/ai-gateway-folder.py) has no API key: it trades the
command's one-time link for a secret of its own (connect), and sends that secret as X-Device-Secret
to ask for work (next, a long poll), hand back what it read (answer) and say goodbye (bye). The
playground's "Don't allow" (deny) is the person signed in, as for any other request.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.auth import Principal, authenticate
from app.tools import device_files

router = APIRouter(prefix="/chat/device", tags=["device folders"])


class ConnectIn(BaseModel):
    link: str = Field(min_length=8, max_length=100)
    folder: str = Field(default="", max_length=300)


class AnswerIn(BaseModel):
    id: str = Field(min_length=8, max_length=100)
    result: dict | None = None
    error: str | None = Field(default=None, max_length=2000)


class DenyIn(BaseModel):
    id: str = Field(min_length=8, max_length=100)


def _folder(secret):
    folder = device_files.by_secret(secret)
    if folder is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "This connection is not known here (the gateway may have restarted). "
                            "Ask in the chat again for a new command.")
    if folder.closed:
        device_files.forget(folder)
        raise HTTPException(status.HTTP_410_GONE, f"The connection has ended: {folder.closed}.")
    return folder


@router.post("/connect")
async def connect(payload: ConnectIn):
    folder = device_files.connect(payload.link, payload.folder)
    if folder is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "This command has been used already or is too old. Ask in the chat again "
                            "for a new one.")
    return {"secret": folder.secret, "folder": folder.name, "skip": device_files.skips(),
            "limits": {"read_lines": device_files.READ_LINES, "read_chars": device_files.READ_CHARS,
                       "list_entries": device_files.LIST_ENTRIES, "search_hits": device_files.SEARCH_HITS,
                       "line_chars": device_files.LINE_CHARS}}


@router.get("/next")
async def next_request(x_device_secret: str = Header(default="")):
    folder = _folder(x_device_secret)
    request = await device_files.next_request(folder)
    if request is None:
        if folder.closed:
            device_files.forget(folder)
            raise HTTPException(status.HTTP_410_GONE, f"The connection has ended: {folder.closed}.")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return {"id": request.id, "op": request.op, "args": request.args}


@router.post("/answer")
async def answer(payload: AnswerIn, x_device_secret: str = Header(default="")):
    folder = _folder(x_device_secret)
    reply = {"error": payload.error} if payload.error else {"result": payload.result or {}}
    return {"taken": device_files.answer(folder, payload.id, reply)}


@router.post("/bye")
async def bye(x_device_secret: str = Header(default="")):
    folder = device_files.by_secret(x_device_secret)
    if folder is not None:
        device_files.bye(folder)
    return {"ok": True}


@router.post("/deny")
async def deny(payload: DenyIn, principal: Principal = Depends(authenticate)):
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a signed-in person can answer this")
    return {"denied": device_files.deny(principal.user.id, payload.id)}
