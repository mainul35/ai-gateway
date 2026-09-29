"""Puts back the model of conversations that a tool's answer overwrote.

A message's model says who produced it, so a map answer is signed "OpenStreetMap" and a picture
search "Picture search". The endpoint that saved it also copied that name onto the conversation,
where model means something quite different - which model to carry on with. The result was a
conversation pointing at a name that is not a model, and reopening it said:

    This conversation used Picture search, which you no longer have access to.

The endpoint no longer does that. This repairs the rows it already wrote: each affected
conversation goes back to the last real model that actually answered in it, or to nothing, which
leaves the playground free to use whatever is selected.

    .venv/bin/python scripts/repair_tool_models.py          # say what would change
    .venv/bin/python scripts/repair_tool_models.py --write  # change it

Safe to run twice; the second time finds nothing.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import session_factory  # noqa: E402
from app.models import Conversation, ConversationMessage  # noqa: E402

# Every name the playground has ever signed a tool's answer with
TOOLS = ("OpenStreetMap", "Picture search", "Video search")


async def main(write):
    async with session_factory()() as session:
        rows = (await session.execute(
            select(Conversation).where(Conversation.model.in_(TOOLS)))).scalars().all()
        if not rows:
            print("nothing to repair")
            return
        for conversation in rows:
            # The last message in it that a real model wrote; tools signed theirs with their own
            # name, so anything in TOOLS is not one
            was = (await session.execute(
                select(ConversationMessage.model)
                .where(ConversationMessage.conversation_id == conversation.id,
                       ConversationMessage.model.isnot(None),
                       ConversationMessage.model.notin_(TOOLS))
                .order_by(ConversationMessage.id.desc()).limit(1))).scalar_one_or_none()
            print(f"  {conversation.id:>5}  {conversation.model!r} -> {was!r}"
                  f"   {(conversation.title or '')[:40]}")
            if write:
                conversation.model = was
        if write:
            await session.commit()
            print(f"\n{len(rows)} conversation(s) repaired")
        else:
            print(f"\n{len(rows)} conversation(s) would be repaired; pass --write to do it")


asyncio.run(main("--write" in sys.argv))
