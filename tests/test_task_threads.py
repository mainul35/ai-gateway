"""app/routers/coding.py: a follow-up question is asked in the thread of the task it follows, and
carries what that thread was asked and found. Nothing is run.

    python -m pytest -q tests/test_task_threads.py
"""
from types import SimpleNamespace

from app.routers import coding


def turn(id, question, found=None, follows=None):
    description = question + (f"{coding.FOLLOWS}{follows}, which was asked:\nearlier" if follows else "")
    return SimpleNamespace(id=id, description=description, summary=found, plan=None)


def test_question_of_leaves_out_what_a_follow_up_carries():
    assert coding.question_of(turn(2, "Now add a test", follows=1).description) == "Now add a test"
    assert coding.question_of("Fix the login page") == "Fix the login page"


def test_follow_up_carries_the_turn_asked_on_and_the_ones_before_it():
    first = turn(1, "How is routing done?", "By the router module.")
    second = turn(5, "Is it tested?", "Only partly.", follows=1)
    third = turn(9, "Which parts are not?", "The fallback.", follows=5)
    context = coding.follow_up_context(third, [first, second])
    assert context.startswith(f"{coding.FOLLOWS}9, which was asked:\nWhich parts are not?")
    assert "What it found:\nThe fallback." in context
    # The earlier turns, oldest first, each without what it carried of the ones before it
    assert context.index("#1 was asked:\nHow is routing done?") < context.index("#5 was asked:\nIs it tested?")
    assert "Only partly." in context and "earlier" not in context
    # The page folds it all away after the question
    assert coding.question_of("Next question" + context) == "Next question"


def test_follow_up_keeps_the_newest_earlier_turns_when_they_do_not_all_fit():
    old = [turn(i, f"question {i}", "x" * 2_400) for i in range(1, 8)]
    context = coding.follow_up_context(turn(8, "last", "found"), old)
    assert "#7 was asked" in context and "#1 was asked" not in context
    assert len(context) < coding.FOLLOW_UP_CHARS + coding.EARLIER_TURNS_CHARS + 2_500
