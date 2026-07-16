"""AI 配置读写（经 store / settings.yaml）。"""

from __future__ import annotations

import os

import pytest

from modules.ai import config as ai_config
from core import store


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """全部用例把应用数据根指到 tmp，并复位 AI 内存，避免污染真实 settings.yaml。"""
    monkeypatch.setattr(store, "_app_base_dir_path", lambda: str(tmp_path))
    monkeypatch.setattr(store, "_app_base_dir", lambda: str(tmp_path))
    store._data_wiped = False
    store.reset_settings_to_defaults(lang="en")
    yield
    # 测完再清一次内存，防止同进程后续文件未隔离时带上 AI 残值
    store.reset_settings_to_defaults(lang="en")


def test_load_defaults(tmp_path):
    data = ai_config.load(str(tmp_path))
    assert data["enabled"] is False
    assert data["consented"] is False
    assert "openai.com" in data["base_url"]
    assert data["api_key"] == ""
    assert data["model_options"] == []


def test_save_and_load_roundtrip(tmp_path):
    path_dir = str(tmp_path)
    saved = ai_config.save(
        path_dir,
        {
            "enabled": True,
            "base_url": "https://example.com/v1",
            "model": "gpt-test",
            "api_key": "sk-plain",
            "extra_prompt": "be brief",
            "consented": True,
            "model_options": ["b-model", "a-model", "b-model", ""],
        },
    )
    assert saved["enabled"] is True
    assert saved["api_key"] == "sk-plain"
    assert saved["model_options"] == ["b-model", "a-model"]
    assert os.path.isfile(store.settings_path())
    assert store.settings_path().startswith(str(tmp_path))

    # 模拟重启：从磁盘重载
    store.reload_settings_from_disk()
    loaded = ai_config.load(path_dir)
    assert loaded["enabled"] is True
    assert loaded["base_url"] == "https://example.com/v1"
    assert loaded["model"] == "gpt-test"
    assert loaded["api_key"] == "sk-plain"
    assert loaded["extra_prompt"] == "be brief"
    assert loaded["consented"] is True
    assert loaded["model_options"] == ["b-model", "a-model"]

    text = open(store.settings_path(), encoding="utf-8").read()
    assert "ai:" in text
    assert "sk-plain" in text
    assert "common:" in text


def test_public_view_hides_key():
    view = ai_config.public_view(
        {
            "enabled": True,
            "base_url": "https://api.example/v1",
            "model": "m",
            "api_key": "secret-key",
            "extra_prompt": "",
            "consented": True,
            "model_options": ["m", "n"],
        }
    )
    assert "api_key" not in view
    assert view["has_key"] is True
    assert view["model"] == "m"
    assert view["model_options"] == ["m", "n"]


def test_public_view_no_key():
    view = ai_config.public_view(
        {
            "enabled": False,
            "base_url": "x",
            "model": "",
            "api_key": "",
            "extra_prompt": "",
            "consented": False,
        }
    )
    assert view["has_key"] is False


def test_get_api_key():
    assert ai_config.get_api_key({"api_key": "abc"}) == "abc"
    assert ai_config.get_api_key({"api_key": "  "}) == ""
    assert ai_config.get_api_key({}) == ""


def test_reset_clears_ai_when_yaml_exists(tmp_path):
    ai_config.save(
        str(tmp_path),
        {"enabled": True, "model": "x", "api_key": "k", "consented": True},
    )
    assert store.get_ai_settings()["enabled"] is True
    assert os.path.isfile(store.settings_path())
    ai_config.reset(str(tmp_path))
    data = ai_config.load(str(tmp_path))
    assert data["enabled"] is False
    assert data["model"] == ""
    assert data["api_key"] == ""
    assert data["consented"] is False


def test_reset_after_defaults_does_not_recreate_yaml(tmp_path):
    ai_config.save(
        str(tmp_path),
        {"enabled": True, "model": "x", "api_key": "k"},
    )
    store.reset_settings_to_defaults(lang="en")
    assert not os.path.isfile(store.settings_path())
    ai_config.reset(str(tmp_path))
    assert not os.path.isfile(store.settings_path())


def test_legacy_ai_json_migrated_then_removed(tmp_path):
    legacy = os.path.join(str(tmp_path), "ai.json")
    with open(legacy, "w", encoding="utf-8") as f:
        f.write(
            '{"enabled": true, "base_url": "https://legacy/v1",'
            ' "model": "legacy-m", "api_key": "legacy-key",'
            ' "extra_prompt": "hi", "consented": true,'
            ' "model_options": ["a"]}'
        )
    data = ai_config.load(str(tmp_path))
    assert data["enabled"] is True
    assert data["model"] == "legacy-m"
    assert data["api_key"] == "legacy-key"
    assert data["base_url"] == "https://legacy/v1"
    assert not os.path.isfile(legacy)
    # 迁移只应写在 tmp 下，不碰真实 LOCALAPPDATA
    assert store.settings_path().startswith(str(tmp_path))
