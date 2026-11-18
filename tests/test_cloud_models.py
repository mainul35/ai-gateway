"""app/cloud_models.py: cloud providers set up from the Models page. Files go to a temporary folder;
nothing is fetched.

    python -m pytest -q tests/test_cloud_models.py
"""
from types import SimpleNamespace

import pytest

from app import access, cloud_models

KEY = "sk-sp-" + "x" * 20


@pytest.fixture
def home(tmp_path, monkeypatch):
    properties = tmp_path / "config.properties"
    properties.write_text("gateway.public.url=https://example.org\n", encoding="utf-8")
    monkeypatch.setenv("CONFIG_FILE", str(properties))
    monkeypatch.setattr(cloud_models, "path", lambda: str(tmp_path / "cloud.yaml"))
    cloud_models._cache["mtime"] = None
    yield properties
    cloud_models._cache["mtime"] = None


def test_openai_base():
    assert cloud_models.openai_base("https://token-plan.maas.qwencloudapi.com/apps/anthropic") \
        == "https://token-plan.maas.qwencloudapi.com/compatible-mode/v1"
    assert cloud_models.openai_base("https://host/apps/anthropic/v1/messages") == "https://host/compatible-mode/v1"
    assert cloud_models.openai_base("https://api.example.com/v1/chat/completions/") == "https://api.example.com/v1"


def test_clean_refuses_what_would_not_work():
    good = {"name": "qwen", "base_url": "https://h/compatible-mode/v1", "models": ["a", {"id": "a"}, {"id": "b", "vision": 1}]}
    assert cloud_models.clean(good)["models"] == [{"id": "a", "vision": False}, {"id": "b", "vision": True}]
    for bad, why in [({**good, "name": "Has space"}, "short name"), ({**good, "base_url": "http://h"}, "https"),
                     ({**good, "models": ["bad id!"]}, "not a model ID"),
                     ({**good, "models": [{"id": "a", "context": 10}]}, "between")]:
        with pytest.raises(cloud_models.CloudError, match=why):
            cloud_models.clean(bad)


def test_saved_provider_becomes_models_and_its_key_stays_out_of_the_file(home, tmp_path):
    with pytest.raises(cloud_models.CloudError, match="API key"):
        cloud_models.save({"name": "qwen", "base_url": "https://h/apps/anthropic", "models": ["qwen3.8-max"]})
    shown = cloud_models.save({"name": "qwen", "label": "Qwen Cloud", "base_url": "https://h/apps/anthropic",
                               "models": [{"id": "qwen3.8-max", "vision": True, "context": 1_000_000}]}, KEY)
    assert shown["key_set"] and "api_key" not in shown and shown["names"] == ["qwen/qwen3.8-max"]
    assert KEY not in (tmp_path / "cloud.yaml").read_text(encoding="utf-8")
    assert f"cloud.qwen.api_key={KEY}" in home.read_text(encoding="utf-8")

    backend = cloud_models.backends()["qwen/qwen3.8-max"]
    assert backend.kind == "cloud" and backend.upstream_model == "qwen3.8-max" and backend.name == "Qwen Cloud"
    assert backend.base_url == "https://h/compatible-mode/v1" and backend.headers() == {"Authorization": f"Bearer {KEY}"}
    assert "vision" in backend.capabilities and backend.context_length == 1_000_000

    # Edited without a key: the key is kept; renamed: it moves with the provider
    cloud_models.save({"name": "qw", "base_url": "https://h/compatible-mode/v1", "models": ["m"]}, renaming_from="qwen")
    assert set(cloud_models.backends()) == {"qw/m"} and cloud_models.api_key("qw") == KEY
    assert "cloud.qwen.api_key" not in home.read_text(encoding="utf-8")

    cloud_models.save({"name": "qw", "base_url": "https://h/compatible-mode/v1", "models": ["m"], "enabled": False},
                      renaming_from="qw")
    assert cloud_models.backends() == {}
    cloud_models.remove("qw")
    assert cloud_models.everything()["providers"] == [] and cloud_models.api_key("qw") == ""


def test_only_admins_until_opened_to_everyone(home):
    cloud_models.save({"name": "qwen", "base_url": "https://h/compatible-mode/v1", "models": ["m"]}, KEY)
    user = SimpleNamespace(role="user", model_access="all", allowed_models="")
    admin = SimpleNamespace(role="admin", model_access="all", allowed_models="")
    person = lambda u: SimpleNamespace(is_master=False, user=u)    # noqa: E731
    assert not access.can_use_model(person(user), "qwen/m")
    assert access.can_use_model(person(admin), "qwen/m")
    assert access.can_use_model(person(user), "llama3:8b")           # local models as before
    cloud_models.save({"name": "qwen", "base_url": "https://h/compatible-mode/v1", "models": ["m"],
                       "everyone": True}, renaming_from="qwen")
    assert access.can_use_model(person(user), "qwen/m")
