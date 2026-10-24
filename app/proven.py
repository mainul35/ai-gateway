"""Which model has been shown to do which work best here, and on what evidence.

Two kinds of evidence, never mixed up:
- measured: tests run on this machine - the coding evaluation (tasks with hidden tests), a real task
  through an agent, a picture read, a fact found in a long text - kept in state/benchmarks.json, since
  what was measured belongs to this machine and its models rather than to the code. A result names
  the gateway model it was run as; a model deleted since is simply not shown.
- feedback: what people said under the answers ("Did the code work?", "Was the translation
  accurate?"), per task, over everybody - for the admin's view; each person's own Auto still learns
  from their own votes alone (app/feedback.py).

The Models page shows, for each kind of work, the model with the best record for it, and on each model
what it has been shown to do.
"""
import json
import logging
import os
from datetime import timedelta

from sqlalchemy import case, func, select

from app.models import AnswerFeedback, utcnow
from app.settings import PROJECT_ROOT

log = logging.getLogger("proven")

BENCHMARKS_FILE = os.path.join(PROJECT_ROOT, "state", "benchmarks.json")
FEEDBACK_DAYS = 90
ENOUGH_VOTES = 3            # verdicts on one task before they say anything about a model

# The kinds of work, in the order the page lists them, and how each is said
TASKS = {
    "agentic": "Agentic coding (a real task, end to end)",
    "code": "Coding (tasks with hidden tests)",
    "vision": "Reading pictures",
    "long": "Long documents and long context",
    "translation": "Translation",
    "reasoning": "Reasoning",
    "general": "General questions",
    "image": "Drawing pictures",
}


def benchmarks():
    """[{model, task, passed, total, seconds, level, note, date, source}] as recorded; [] without the file."""
    try:
        with open(BENCHMARKS_FILE, encoding="utf-8") as f:
            return [r for r in json.load(f).get("results") or [] if r.get("model") and r.get("task")]
    except (OSError, ValueError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("could not read %s: %s", BENCHMARKS_FILE, e)
        return []


def speeds():
    """{model: {"short": tok/s, "long": tok/s, "note"}} - writing speed as last measured."""
    try:
        with open(BENCHMARKS_FILE, encoding="utf-8") as f:
            return json.load(f).get("speeds") or {}
    except (OSError, ValueError):
        return {}


async def feedback_by_task(session):
    """{model: {task: {"good", "votes"}}} over everybody's verdicts in the last FEEDBACK_DAYS."""
    good = func.sum(case((AnswerFeedback.helpful.is_(True), 1), (AnswerFeedback.accurate.is_(True), 1), else_=0))
    votes = func.sum(case((AnswerFeedback.helpful.is_not(None), 1), (AnswerFeedback.accurate.is_not(None), 1), else_=0))
    rows = await session.execute(
        select(AnswerFeedback.model, AnswerFeedback.category, good, votes)
        .where(AnswerFeedback.model.is_not(None), AnswerFeedback.updated_at >= utcnow() - timedelta(days=FEEDBACK_DAYS))
        .group_by(AnswerFeedback.model, AnswerFeedback.category))
    found = {}
    for model, task, good_votes, all_votes in rows.all():
        if all_votes:
            found.setdefault(model, {})[task or "general"] = {"good": int(good_votes or 0), "votes": int(all_votes)}
    return found


def _measured_line(r):
    score = f"{r['passed']}/{r['total']}" if r.get("total") else ""
    took = r.get("seconds")
    took = (f"{took / 60:.0f} min" if took >= 120 else f"{took:.0f} s") if took else ""
    return ", ".join(x for x in (score, took, r.get("note") or "") if x)


def summary(installed, feedback):
    """{"best_for": [{task, label, model, evidence, kind}], "proven": {model: [{task, label, text, kind}]},
    "speeds": {model: {...}}} for the models in `installed`."""
    results = [r for r in benchmarks() if r["model"] in installed]
    proven = {}
    for r in results:
        proven.setdefault(r["model"], []).append(
            {"task": r["task"], "label": TASKS.get(r["task"], r["task"]), "kind": "measured",
             "text": _measured_line(r), "date": r.get("date", ""), "source": r.get("source", "")})
    for model, tasks in feedback.items():
        if model not in installed:
            continue
        for task, counts in tasks.items():
            if counts["votes"] >= ENOUGH_VOTES:
                proven.setdefault(model, []).append(
                    {"task": task, "label": TASKS.get(task, task), "kind": "feedback",
                     "text": f"{counts['good']} of {counts['votes']} answers judged good"})

    best = []
    for task, label in TASKS.items():
        measured = [r for r in results if r["task"] == task and r.get("total")]
        if measured:
            # Most right first; on a tie the harder test (`level`: a fact found in 223K tokens beats one
            # in 118K), then the quicker one
            top = max(measured, key=lambda r: (r["passed"] / r["total"], r.get("level") or 0,
                                               -(r.get("seconds") or 1e9)))
            best.append({"task": task, "label": label, "model": top["model"], "kind": "measured",
                         "evidence": _measured_line(top) + (f" ({top['source']})" if top.get("source") else "")})
            continue
        judged = [(m, c[task]) for m, c in feedback.items()
                  if m in installed and task in c and c[task]["votes"] >= ENOUGH_VOTES]
        if judged:
            model, counts = max(judged, key=lambda pair: (pair[1]["good"] / pair[1]["votes"], pair[1]["votes"]))
            best.append({"task": task, "label": label, "model": model, "kind": "feedback",
                         "evidence": f"{counts['good']} of {counts['votes']} answers judged good"})
    return {"best_for": best, "proven": proven,
            "speeds": {m: s for m, s in speeds().items() if m in installed}}
