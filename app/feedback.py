"""What a person said about their answers: helpful or not, accurate or not, per model and kind of question.

The playground asks under each answer. The verdicts are read back here for Auto (app/tools/chooser.py),
which leans away from a model this person keeps marking inaccurate for some kind of question and
towards one they keep marking accurate. Only ever their own: one person's careless or mistaken
verdicts would otherwise steer everybody's answers, so each person's votes shape their Auto alone.
"""
from sqlalchemy import case, func, select

from app.models import AnswerFeedback


async def by_model(session, since, user_id):
    """{model: {"all": counts, <category>: counts}} of one person's verdicts; {} for nobody (the master key)."""
    if user_id is None:
        return {}
    yes = lambda column: func.sum(case((column.is_(True), 1), else_=0))
    no = lambda column: func.sum(case((column.is_(False), 1), else_=0))
    rows = await session.execute(
        select(AnswerFeedback.model, AnswerFeedback.category,
               yes(AnswerFeedback.accurate), no(AnswerFeedback.accurate),
               yes(AnswerFeedback.helpful), no(AnswerFeedback.helpful))
        .where(AnswerFeedback.user_id == user_id, AnswerFeedback.updated_at >= since,
               AnswerFeedback.model.is_not(None))
        .group_by(AnswerFeedback.model, AnswerFeedback.category))
    found = {}
    for model, category, accurate, inaccurate, helpful, unhelpful in rows.all():
        counts = {"accurate": accurate or 0, "inaccurate": inaccurate or 0,
                  "helpful": helpful or 0, "unhelpful": unhelpful or 0}
        entry = found.setdefault(model, {"all": {k: 0 for k in counts}})
        for key, value in counts.items():
            entry["all"][key] += value
        if category:
            entry[category] = counts
    return found
