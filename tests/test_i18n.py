"""Tests for the i18n system."""
from __future__ import annotations

import pytest

from maiocr import i18n


def test_supported_languages_present():
    assert "en" in i18n.SUPPORTED_LANGUAGES
    assert "zh-CN" in i18n.SUPPORTED_LANGUAGES


def test_default_translator_falls_back_to_key():
    i18n.set_default_language("en")
    assert i18n.t("app.name") == "MaiOCR"
    assert i18n.t("__missing__") == "__missing__"


def test_chinese_translation():
    tr = i18n.Translator("zh-CN")
    assert tr("app.name") == "MaiOCR"
    # Chinese status line for "service: running" should be in Chinese
    val = tr("status.service_running")
    assert val != "status.service_running"  # translated
    assert any('\u4e00' <= c <= '\u9fff' for c in val)  # contains a CJK char


def test_format_kwargs():
    tr = i18n.Translator("zh-CN")
    msg = tr("status.gpu_device", device="RTX 4060")
    assert "RTX 4060" in msg
    assert "GPU" in msg or "gpu" in msg.lower() or "设备" in msg


def test_fallback_when_key_missing_in_translation():
    tr = i18n.Translator("zh-CN")
    # unknown key falls back to the key itself
    assert tr("not.a.real.key") == "not.a.real.key"


def test_resolve_language_validates():
    assert i18n.resolve_language("en") == "en"
    assert i18n.resolve_language("zh-CN") == "zh-CN"
    assert i18n.resolve_language("xx") == "en"
    assert i18n.resolve_language(None) == "en"


def test_set_default_language_ignores_unknown():
    i18n.set_default_language("en")
    i18n.set_default_language("xx")
    assert i18n.get_default_language() == "en"


def test_list_languages_returns_pairs():
    pairs = i18n.list_languages()
    codes = [c for c, _ in pairs]
    assert "en" in codes
    assert "zh-CN" in codes
