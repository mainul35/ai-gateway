"""app/github_access.py: an owner can hold several tokens, each added beside the others, swapped
only by Replace, and for a repository the one that reaches it is used. GitHub is not called:
check() and the repository question are replaced.

    python -m pytest -q tests/test_github_tokens.py
"""
import asyncio

import pytest

from app import config_writer, github_access, settings
from utils import config

USER_A = "github_pat_" + "a" * 30           # the person's token for some repositories
USER_B = "github_pat_" + "b" * 30           # the person's token for others
ORG = "github_pat_" + "o" * 30
CLASSIC = "ghp_" + "c" * 30
SEES = {USER_A: ["person"], USER_B: ["person"], ORG: ["Acme"], CLASSIC: ["Acme", "person"]}
REACHES = {USER_A: {"person/one"}, USER_B: {"person/two"}}


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

    async def reaches(value, owner, repo, purpose):
        return f"{owner}/{repo}" in REACHES.get(value, set())
    monkeypatch.setattr(github_access, "check", check)
    monkeypatch.setattr(github_access, "_reaches", reaches)
    return saved


def run(coroutine):
    return asyncio.run(coroutine)


def save(owner, value, replace=None):
    return run(github_access.save(owner, value, replace=replace))


def test_a_second_token_for_the_same_owner_is_added_beside_the_first(stored):
    assert save("person", USER_A)["id"] == 1
    assert save("person", USER_B)["id"] == 2
    assert stored == {"github.token.person": USER_A, "github.token.person.2": USER_B}
    assert [t["id"] for t in github_access.owners()[0]["tokens"]] == [1, 2]


def test_each_repository_gets_the_token_that_reaches_it(stored):
    save("person", USER_A)
    save("person", USER_B)
    assert run(github_access.repo_token("person", "one", "write")) == USER_A
    assert run(github_access.repo_token("person", "two", "write")) == USER_B
    assert run(github_access.repo_token("person", "three", "write")) == USER_A     # none does: the first


def test_replace_swaps_only_the_token_it_names(stored):
    save("person", USER_A)
    save("person", USER_B)
    other = "github_pat_" + "x" * 30
    SEES[other] = ["person"]
    save("PERSON", other, replace=2)
    assert stored == {"github.token.person": USER_A, "github.token.person.2": other}
    with pytest.raises(ValueError, match="already saved"):
        save("person", USER_A)


def test_an_organisations_token_goes_under_the_organisation(stored):
    save("person", USER_A)
    assert save("", ORG)["owner"] == "Acme"                         # found from the token
    with pytest.raises(ValueError, match="it is for Acme"):
        save("person", ORG)
    assert set(stored) == {"github.token.person", "github.token.Acme"}


def test_a_token_that_sees_several_owners_needs_one_named(stored):
    with pytest.raises(ValueError, match="Acme, person"):
        save("", CLASSIC)
    assert save("acme", CLASSIC)["owner"] == "Acme"                # GitHub's spelling


def test_removing_the_first_leaves_the_others_in_use(stored):
    save("person", USER_A)
    save("person", USER_B)
    github_access.remove("person", 1)
    assert github_access.token("person", "write") == USER_B
    assert save("person", USER_A)["id"] == 1                       # the free number is used again


def test_a_token_has_a_name_that_replace_keeps(stored):
    save("person", USER_A)
    found = run(github_access.save("person", USER_B, name="vsd  auth server"))
    assert found["name"] == "vsd auth server" and stored["github.tokenname.person.2"] == "vsd auth server"
    other = "github_pat_" + "y" * 30
    SEES[other] = ["person"]
    save("person", other, replace=2)
    assert github_access.chosen("person", 2)["name"] == "vsd auth server"
    github_access.rename("person", 2, "")
    assert "github.tokenname.person.2" not in stored
    with pytest.raises(ValueError, match="name"):
        run(github_access.save("person", "github_pat_" + "z" * 30, name="bad/name"))


def test_a_project_that_names_a_token_gets_that_one_or_an_error(stored):
    save("person", USER_A)
    save("person", USER_B)
    # Even for a repository the first one reaches, the project's own token is used
    assert run(github_access.repo_token("person", "one", "write", pinned=2)) == USER_B
    github_access.remove("person", 2)
    with pytest.raises(github_access.Missing, match="removed"):
        run(github_access.repo_token("person", "one", "write", pinned=2))
    assert "github.tokenname.person.2" not in stored
