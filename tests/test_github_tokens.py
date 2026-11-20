"""app/github_access.py: an owner can hold several tokens, each added beside the others, swapped
only by Replace; for a repository the one that reaches it is used, or the one its project names.
Tokens are kept sealed, and ones left in config.properties move into the store. Nothing is called:
GitHub, the database and the key are replaced.

    python -m pytest -q tests/test_github_tokens.py
"""
import asyncio

import pytest

from app import config_writer, github_access, secretbox, settings
from utils import config

USER_A = "github_pat_" + "a" * 30           # the person's token for some repositories
USER_B = "github_pat_" + "b" * 30           # the person's token for others
ORG = "github_pat_" + "o" * 30
CLASSIC = "ghp_" + "c" * 30
SEES = {USER_A: ["person"], USER_B: ["person"], ORG: ["Acme"], CLASSIC: ["Acme", "person"]}
REACHES = {USER_A: {"person/one"}, USER_B: {"person/two"}}


class FakeStore:
    def __init__(self):
        self.rows_held = {}                 # (owner, number) -> (name, sealed)

    async def rows(self):
        return [(o, n, name, sealed) for (o, n), (name, sealed) in self.rows_held.items()]

    async def put(self, owner, number, name, sealed, was_owner=None):
        old = self.rows_held.pop((was_owner or owner, number), (name, None))
        self.rows_held[(owner, number)] = (name, sealed if sealed is not None else old[1])

    async def drop(self, owner, number):
        self.rows_held.pop((owner, number), None)

    def plain(self):
        """What is held, unsealed: {(owner, number): (name, token)}."""
        return {k: (name, secretbox.unseal(sealed)) for k, (name, sealed) in self.rows_held.items()}


@pytest.fixture
def stored(monkeypatch):
    props = {}
    monkeypatch.setattr(config, "properties", lambda: dict(props))
    monkeypatch.setattr(settings, "get", lambda name, *a, **k: props.get(name))
    monkeypatch.setattr(config_writer, "remove_key", lambda name: props.pop(name, None))
    monkeypatch.setattr(secretbox, "seal", lambda plain: "v1:" + plain[::-1])
    monkeypatch.setattr(secretbox, "unseal", lambda sealed: sealed[3:][::-1])
    store = FakeStore()
    store.props = props
    monkeypatch.setattr(github_access, "store", store)
    monkeypatch.setattr(github_access, "_tokens", [])

    async def check(owner, value):
        owners = SEES[value]
        mine = [o for o in owners if owner and o.lower() == owner.lower()]
        return {"login": "person", "owners": owners, "repositories": 3 if mine else 0,
                "can_push": bool(mine), "private": 0}

    async def reaches(value, owner, repo, purpose):
        return f"{owner}/{repo}" in REACHES.get(value, set())
    monkeypatch.setattr(github_access, "check", check)
    monkeypatch.setattr(github_access, "_reaches", reaches)
    return store


def run(coroutine):
    return asyncio.run(coroutine)


def save(owner, value, replace=None, name=None):
    return run(github_access.save(owner, value, replace=replace, name=name))


def test_tokens_are_kept_sealed_and_shown_masked(stored):
    save("person", USER_A)
    name, sealed = stored.rows_held[("person", 1)]
    assert USER_A not in sealed
    shown = github_access.owners()[0]["tokens"][0]["token"]
    assert shown == "github_pat_••••aaaa" and USER_A not in shown


def test_tokens_left_in_the_file_move_into_the_store(stored):
    stored.props.update({"github.token.person": USER_A, "github.token.person.2": USER_B,
                         "github.tokenname.person.2": "vsd", "coding.github.token.other": CLASSIC})
    run(github_access.load())
    assert stored.plain() == {("person", 1): ("", USER_A), ("person", 2): ("vsd", USER_B)}
    assert stored.props == {"coding.github.token.other": CLASSIC}      # an older setting stays where it is
    assert github_access.tokens("person", "write") == [USER_A, USER_B]


def test_a_second_token_for_the_same_owner_is_added_beside_the_first(stored):
    assert save("person", USER_A)["id"] == 1
    assert save("person", USER_B)["id"] == 2
    assert stored.plain() == {("person", 1): ("", USER_A), ("person", 2): ("", USER_B)}
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
    assert stored.plain() == {("person", 1): ("", USER_A), ("person", 2): ("", other)}
    with pytest.raises(ValueError, match="already saved"):
        save("person", USER_A)


def test_an_organisations_token_goes_under_the_organisation(stored):
    save("person", USER_A)
    assert save("", ORG)["owner"] == "Acme"                         # found from the token
    with pytest.raises(ValueError, match="it is for Acme"):
        save("person", ORG)
    assert set(stored.rows_held) == {("person", 1), ("Acme", 1)}


def test_a_token_that_sees_several_owners_needs_one_named(stored):
    with pytest.raises(ValueError, match="Acme, person"):
        save("", CLASSIC)
    assert save("acme", CLASSIC)["owner"] == "Acme"                # GitHub's spelling


def test_removing_the_first_leaves_the_others_in_use(stored):
    save("person", USER_A)
    save("person", USER_B)
    run(github_access.remove("person", 1))
    assert github_access.token("person", "write") == USER_B
    assert save("person", USER_A)["id"] == 1                       # the free number is used again


def test_a_token_has_a_name_that_replace_keeps(stored):
    save("person", USER_A)
    assert save("person", USER_B, name="vsd  auth server")["name"] == "vsd auth server"
    other = "github_pat_" + "y" * 30
    SEES[other] = ["person"]
    save("person", other, replace=2)
    assert github_access.chosen("person", 2)["name"] == "vsd auth server"
    run(github_access.rename("person", 2, ""))
    assert stored.plain()[("person", 2)] == ("", other)
    with pytest.raises(ValueError, match="name"):
        save("person", "github_pat_" + "z" * 30, name="bad/name")


def test_a_project_that_names_a_token_gets_that_one_or_an_error(stored):
    save("person", USER_A)
    save("person", USER_B)
    # Even for a repository the first one reaches, the project's own token is used
    assert run(github_access.repo_token("person", "one", "write", pinned=2)) == USER_B
    run(github_access.remove("person", 2))
    with pytest.raises(github_access.Missing, match="removed"):
        run(github_access.repo_token("person", "one", "write", pinned=2))


def test_a_refused_token_is_named_as_the_cause():
    from app.coding import workspace
    assert workspace._refused("remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/o/r.git/'")
    assert workspace._refused("remote: Permission to o/r.git denied to someone.")
    assert not workspace._refused("fatal: couldn't find remote ref feature/x")
