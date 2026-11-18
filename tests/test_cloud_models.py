"""app/cloud_models.py: cloud providers set up from the Models page and kept in the database. The
database is replaced by a list; nothing is fetched.

    python -m pytest -q tests/test_cloud_models.py
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import access, cloud_models, secretbox

KEY = "sk-sp-" + "x" * 20


class Session:
    """Just what cloud_models asks of a database session."""
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    async def delete(self, row):
        self.rows.remove(row)

    async def commit(self):
        pass


@pytest.fixture
def db(monkeypatch, tmp_path):
    session = Session()

    async def rows(_session):
        return sorted(session.rows, key=lambda r: r.name or "")
    monkeypatch.setattr(cloud_models, "_rows", rows)
    monkeypatch.setattr(secretbox, "KEY_FILE", str(tmp_path / "encryption.key"))
    monkeypatch.setattr(secretbox, "_key", None)
    monkeypatch.setattr(cloud_models, "_snapshot", {"providers": []})
    return session


def run(coroutine):
    return asyncio.run(coroutine)


def test_openai_base():
    assert cloud_models.openai_base("https://token-plan.maas.qwencloudapi.com/apps/anthropic") \
        == "https://token-plan.maas.qwencloudapi.com/compatible-mode/v1"
    assert cloud_models.openai_base("https://host/apps/anthropic/v1/messages") == "https://host/compatible-mode/v1"
    assert cloud_models.openai_base("https://api.example.com/v1/chat/completions/") == "https://api.example.com/v1"


def test_clean_refuses_what_would_not_work():
    good = {"name": "qwen", "base_url": "https://h/compatible-mode/v1", "models": ["a", {"id": "a"}, {"id": "b", "vision": 1}]}
    assert [m["id"] for m in cloud_models.clean(good)["models"]] == ["a", "b"]
    assert cloud_models.clean(good)["models"][1] == {"id": "b", "vision": True, "tools": True, "thinking": False}
    for bad, why in [({**good, "name": "Has space"}, "short name"), ({**good, "base_url": "http://h"}, "https"),
                     ({**good, "models": ["bad id!"]}, "not a model ID"),
                     ({**good, "models": [{"id": "a", "context": 10}]}, "between")]:
        with pytest.raises(cloud_models.CloudError, match=why):
            cloud_models.clean(bad)


def test_saved_provider_becomes_models_with_its_key_sealed(db):
    with pytest.raises(cloud_models.CloudError, match="API key"):
        run(cloud_models.save(db, {"name": "qwen", "base_url": "https://h/apps/anthropic", "models": ["m"]}))
    assert db.rows == []
    shown = run(cloud_models.save(db, {"name": "qwen", "label": "Qwen Cloud", "base_url": "https://h/apps/anthropic",
                                       "models": [{"id": "qwen3.8-max", "vision": True, "context": 1_000_000}]}, KEY))
    assert shown["key_set"] and "key" not in shown and shown["names"] == ["qwen/qwen3.8-max"]
    assert KEY not in db.rows[0].key_sealed and secretbox.unseal(db.rows[0].key_sealed) == KEY

    backend = cloud_models.backends()["qwen/qwen3.8-max"]
    assert backend.kind == "cloud" and backend.upstream_model == "qwen3.8-max" and backend.name == "Qwen Cloud"
    assert backend.base_url == "https://h/compatible-mode/v1" and backend.headers() == {"Authorization": f"Bearer {KEY}"}
    assert backend.capabilities == {"completion", "vision", "tools"} and backend.context_length == 1_000_000

    # Changed and renamed without a key: the key is kept
    run(cloud_models.save(db, {"name": "qw", "base_url": "https://h/compatible-mode/v1",
                               "models": [{"id": "m", "tools": False}]}, renaming_from="qwen"))
    assert set(cloud_models.backends()) == {"qw/m"} and cloud_models.backends()["qw/m"].capabilities == {"completion"}
    assert run(cloud_models.saved_key(db, "qw")) == KEY

    with pytest.raises(cloud_models.CloudError, match="already"):
        run(cloud_models.save(db, {"name": "qw", "base_url": "https://h/v1"}, KEY))
    run(cloud_models.save(db, {"name": "qw", "base_url": "https://h/v1", "models": ["m"], "enabled": False},
                          renaming_from="qw"))
    assert cloud_models.backends() == {}
    run(cloud_models.remove(db, "qw"))
    assert run(cloud_models.everything(db)) == {"providers": []}


def test_who_may_use_them_and_whether_auto_may_pick_them(db):
    run(cloud_models.save(db, {"name": "qwen", "base_url": "https://h/v1", "models": ["m"]}, KEY))
    user = SimpleNamespace(role="user", model_access="all", allowed_models="")
    admin = SimpleNamespace(role="admin", model_access="all", allowed_models="")
    person = lambda u: SimpleNamespace(is_master=False, user=u)    # noqa: E731
    assert not access.can_use_model(person(user), "qwen/m") and access.can_use_model(person(admin), "qwen/m")
    assert access.can_use_model(person(user), "llama3:8b")           # local models as before
    assert not cloud_models.auto_may_pick("qwen/m") and cloud_models.auto_may_pick("llama3:8b")
    run(cloud_models.save(db, {"name": "qwen", "base_url": "https://h/v1", "models": ["m"],
                               "everyone": True, "auto_pick": True}, renaming_from="qwen"))
    assert access.can_use_model(person(user), "qwen/m") and cloud_models.auto_may_pick("qwen/m")
