"""Tests for the persistent user settings module."""
from __future__ import annotations

import pytest

from maiocr import i18n, settings as settings_mod


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch, tmp_path):
    """Each test gets a fresh config file and a reset module state."""
    fake = tmp_path / "settings.yml"
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", fake)
    settings_mod._CURRENT = None
    i18n.set_default_language(i18n.DEFAULT_LANGUAGE)
    yield
    settings_mod._CURRENT = None
    i18n.set_default_language(i18n.DEFAULT_LANGUAGE)


def test_default_settings(tmp_path):
    s = settings_mod.get_settings(reload=True)
    assert s.language == "en"
    assert s.auto_release_vram is False
    assert s.prefer_gpu is True
    assert tmp_path.joinpath("settings.yml").exists()


def test_settings_persist_across_load():
    s = settings_mod.get_settings(reload=True)
    s.language = "zh-CN"
    s.auto_release_vram = True
    s.auto_release_seconds = 30
    s.save()
    settings_mod._CURRENT = None
    s2 = settings_mod.get_settings(reload=True)
    assert s2.language == "zh-CN"
    assert s2.auto_release_vram is True
    assert s2.auto_release_seconds == 30


def test_unknown_language_falls_back(tmp_path):
    tmp_path.joinpath("settings.yml").write_text("language: xx\n")
    s = settings_mod.get_settings(reload=True)
    assert s.language == "en"


def test_save_settings_updates_i18n():
    s = settings_mod.get_settings(reload=True)
    s.language = "zh-CN"
    settings_mod.save_settings(s)
    assert i18n.get_default_language() == "zh-CN"


def test_update_ignores_unknown_keys():
    s = settings_mod.get_settings(reload=True)
    s.update(language="zh-CN", unknown_key="ignored")
    assert s.language == "zh-CN"


def test_load_custom_path_preserves_target_file(tmp_path):
    custom_cfg = tmp_path / "custom_subdir" / "my_settings.yml"
    custom_cfg.parent.mkdir(parents=True)
    # Write only a subset of keys (missing new defaults)
    custom_cfg.write_text("language: zh-CN\nprefer_gpu: false\n")

    loaded = settings_mod.Settings.load(custom_cfg)
    assert loaded.language == "zh-CN"
    assert loaded.prefer_gpu is False
    # Verify defaults were saved to custom_cfg, not global CONFIG_FILE
    assert custom_cfg.exists()
    content = custom_cfg.read_text()
    assert "log_level" in content

