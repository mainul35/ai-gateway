"""app/github_access.py: a token is saved under its own owner, and an owner's token is only swapped
when that is asked for. GitHub is not called: check() is replaced.

    python -m pytest -q tests/test_github_tokens.py
"""
import asyncio

import pytest

from app import config_writer, github_access, settings
from utils import config

USER_TOKEN = "github_pat_" + "u" * 30
ORG_TOKEN = "github_pat_" + "o" * 30
CLASSIC = "ghp_" + "c" * 30
SEES = {USER_TOKEN: ["person"], ORG_TOKEN: ["Acme"], CLASSIC: ["Acme", "person"]}


@pytest.fixture
def stored(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "properties", lambda: dict(saved))
    monkeypatch.setattr(settings, "get", lambda name, *a, **k: saved.get(name))
    monkeypatch.setattr(config_writer, "write_secret", lambda name, value: saved.__setitem__(name, value))
    monkeypatch.setattr(config_writer, "remove_key", lambda name: saved.pop(name, None))

    async def check(owner, value):
        owners = SEES[value]
        mine = [o for o in owners if owner and o.lower() == owner.lower()]
        return {"login": "person", "owners": owners, "repositories": 3 if mine else 0,
                "can_push": bool(mine), "private": 0}
    monkeypatch.setattr(github_access, "check", check)
    return saved


def save(owner, value, replace=False):
    return asyncio.run(github_access.save(owner, value, replace=replace))


def test_an_organisations_token_is_added_beside_the_persons_not_over_it(stored):
    assert save("person", USER_TOKEN)["owner"] == "person"
    assert save("", ORG_TOKEN)["owner"] == "Acme"                  # found from the token
    assert stored == {"github.token.person": USER_TOKEN, "github.token.Acme": ORG_TOKEN}


def test_an_owner_with_a_token_keeps_it_unless_replace_is_asked(stored):
    save("person", USER_TOKEN)
    other = "github_pat_" + "x" * 30
    SEES[other] = ["person"]
    with pytest.raises(github_access.Taken, match="Replace"):
        save("PERSON", other)
    assert stored["github.token.person"] == USER_TOKEN
    save("person", other, replace=True)
    assert stored == {"github.token.person": other}


def test_a_token_for_another_owner_names_that_owner(stored):
    with pytest.raises(ValueError, match="it is for Acme"):
        save("person", ORG_TOKEN)
    assert stored == {}


def test_a_token_that_sees_several_owners_needs_one_named(stored):
    with pytest.raises(ValueError, match="Acme, person"):
        save("", CLASSIC)
    assert save("acme", CLASSIC)["owner"] == "Acme"                # GitHub's spelling
    assert stored == {"github.token.Acme": CLASSIC}
