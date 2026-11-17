"""app/tools/repos.py: which GitHub repositories a message names, and what a tool accepts as one and as
a path in it. No network, no git.

    python -m pytest -q tests/test_repos.py
"""
import pytest

from app.tools import repos


def test_mentioned_in_order_without_repeats():
    text = ("look at https://github.com/Some-Owner/my.repo and https://github.com/other/thing.git, "
            "then https://github.com/Some-Owner/my.repo/blob/main/app.py and github.com/no-scheme")
    assert repos.mentioned(text) == ["Some-Owner/my.repo", "other/thing"]
    assert repos.mentioned(None) == []
    assert repos.mentioned("https://gist.github.com/a/b https://github.com/settings") == []


@pytest.mark.parametrize("value, expected", [
    ("https://github.com/o/r", ("o", "r", None)),
    ("https://github.com/o/r.git", ("o", "r", None)),
    ("https://www.github.com/o/r/tree/develop/app/tools", ("o", "r", "develop")),
    ("https://github.com/o/r/blob/v1.2/README.md", ("o", "r", "v1.2")),
    ("o/r", ("o", "r", None)),
    ("  o/r.git ", ("o", "r", None)),
])
def test_parse(value, expected):
    assert repos.parse(value) == expected


@pytest.mark.parametrize("value", ["", "just-a-name", "https://example.com/o/r", "o/..", "-x/r", "o/r/s"])
def test_parse_refuses(value):
    with pytest.raises(repos.RepoError):
        repos.parse(value)


@pytest.mark.parametrize("path", ["../x", "a/../../b", "-n", ":(glob)*", "a\nb"])
def test_paths_stay_in_the_repository(path):
    with pytest.raises(repos.RepoError):
        repos._clean_path(path)


def test_clean_path_trims():
    assert repos._clean_path("/app/tools/") == "app/tools"
    assert repos._clean_path(None) == ""


def test_clone_path_stays_inside(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_EXPLORE_DIR", str(tmp_path))
    assert repos._path("public", "o", "r").startswith(str(tmp_path))
