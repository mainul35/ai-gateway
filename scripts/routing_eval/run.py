"""Scores the playground's routing on a fixed set of messages: the current three questions against the
understanding step (app/tools/understand.py), both asking the live router model.

Run on the server, from the project directory, with every tool switched on:

    .venv/bin/python scripts/routing_eval/run.py [--model qwen3-1.7b-cpu:latest] [--only new|old]

Each case in cases.json has the message, optional attachments and history, the action that is right
("want", or a list of acceptable ones) and optionally the kind of work ("work"). It talks to Ollama
directly (OLLAMA_URL, default 127.0.0.1:11434), so the gateway itself is not involved.
"""
import argparse
import asyncio
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.tools import router, understand  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
TOOLS = {"web_search": True, "image_generation": True, "maps": True, "photo_blur": True}


async def ask(client, body, model):
    body = {**body, "model": model}
    try:
        response = await client.post(f"{OLLAMA}/v1/chat/completions", json=body, timeout=120)
        return response.json()["choices"][0]["message"].get("content")
    except Exception as e:      # an unanswered call is what the fallback is for
        print("   call failed:", e, file=sys.stderr)
        return None


async def old_route(client, model, message, has_images, history):
    """What /chat/route did before the understanding step, step for step."""
    allowed = router.candidates(TOOLS, has_images)
    changes = (router.EDIT, router.CLEAN, router.BLUR, router.BACKDROP)

    async def action():
        found = router.clean_answer(await ask(client, router.build_request(message, allowed, history), model), allowed)
        settled, _ = router.settle_action(found, message, allowed, has_images)
        return settled or router.by_keywords(message, allowed, has_images)

    async def kind():
        found = router.clean_category(await ask(client, router.build_category_request(message, history), model), has_images)
        return router.settle_category(found, message, has_images)[0]

    async def picture():
        if not (has_images and any(a in allowed for a in changes)):
            return None
        return router.read_picture_answer(await ask(client, router.build_picture_request(message, history), model))

    chosen, category, wanted = await asyncio.gather(action(), kind(), picture())
    chosen, _ = router.settle_picture(chosen, "model", wanted, message, allowed)
    return chosen, category, None


async def new_route(client, model, message, attachments, has_images, history):
    """The understanding step, with the one-word tool question asked alongside as a second opinion."""
    allowed = router.candidates(TOOLS, has_images)

    async def second():
        found = router.clean_answer(await ask(client, router.build_request(message, allowed, history), model), allowed)
        return router.settle_action(found, message, allowed, has_images)[0]

    answer, other = await asyncio.gather(
        ask(client, understand.build_request(message, attachments, history), model), second())
    reading = understand.read_answer(answer)
    if reading is None:
        return router.by_keywords(message, allowed, has_images), router.category_by_keywords(message, has_images), None
    return (understand.decide(reading, allowed, has_images, message, other),
            understand.category(reading, has_images), reading)


def acceptable(value, wanted):
    return value in (wanted if isinstance(wanted, list) else [wanted])


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3-1.7b-cpu:latest")
    parser.add_argument("--only", choices=("old", "new"))
    parser.add_argument("--cases", default="cases.json", help="cases.json, or holdout.json: messages the prompt was never tuned on")
    args = parser.parse_args()
    cases = json.load(open(os.path.join(HERE, args.cases), encoding="utf-8"))
    which = [args.only] if args.only else ["old", "new"]
    score = {w: {"action": 0, "work": 0, "work_cases": 0, "seconds": 0.0} for w in which}
    async with httpx.AsyncClient() as client:
        for number, case in enumerate(cases, 1):
            attachments = case.get("attachments") or []
            has_images = any(a.get("kind") == "picture" for a in attachments)
            history = router.plain_history(case.get("history") or [])
            line = [f"{number:2} {case['message'][:58]!r:62} want {case['want']!s:16}"]
            for w in which:
                started = time.monotonic()
                if w == "old":
                    action, kind, reading = await old_route(client, args.model, case["message"], has_images, history)
                else:
                    action, kind, reading = await new_route(client, args.model, case["message"], attachments,
                                                            has_images, history)
                took = time.monotonic() - started
                s = score[w]
                s["seconds"] += took
                right = acceptable(action, case["want"])
                s["action"] += right
                mark = "ok " if right else "XX "
                if case.get("work"):
                    s["work_cases"] += 1
                    work_right = acceptable(kind, ["vision" if x == "picture" else x for x in
                                                   (case["work"] if isinstance(case["work"], list) else [case["work"]])])
                    s["work"] += work_right
                    mark += f"{kind}{'' if work_right else '!'}"
                line.append(f"| {w} {mark} {action:8} {took:4.1f}s")
                if w == "new" and not right and reading:
                    line.append(f"\n      read as: {json.dumps(reading)}")
            print(" ".join(line), flush=True)
    print()
    for w in which:
        s = score[w]
        print(f"{w}: right tool {s['action']}/{len(cases)}, right kind of work {s['work']}/{s['work_cases']}, "
              f"{s['seconds'] / len(cases):.1f} s a message on average")


if __name__ == "__main__":
    asyncio.run(main())
