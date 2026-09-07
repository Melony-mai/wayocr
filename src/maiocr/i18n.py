"""Localization for MaiOCR.

A lightweight, dependency-free i18n system based on nested dictionaries
loaded from YAML files. Languages are switched at runtime; the active
language is stored in :class:`maiocr.settings.Settings` so it persists
across restarts.

Adding a new language is a matter of dropping a YAML file in
``maiocr/locales/<code>.yml`` and listing the new code in
:data:`SUPPORTED_LANGUAGES`.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import yaml


SUPPORTED_LANGUAGES: Dict[str, str] = {
    "en": "English",
    "zh-CN": "简体中文",
}
DEFAULT_LANGUAGE = "en"
FALLBACK_LANGUAGE = "en"


LOCALES_DIR: Path = Path(__file__).resolve().parent / "locales"

_CACHE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.RLock()


def _load(lang: str) -> Dict[str, Any]:
    if lang in _CACHE:
        return _CACHE[lang]
    path = LOCALES_DIR / f"{lang}.yml"
    if not path.exists():
        _CACHE[lang] = {}
        return _CACHE[lang]
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        data = {}
    _CACHE[lang] = data
    return data


def _lookup(d: Dict[str, Any], key: str) -> Optional[str]:
    """Resolve a dotted key inside a nested dict."""
    node: Any = d
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


class Translator:
    """A thread-local-aware translation handle.

    The currently active language comes from the user's settings. If
    the user has not configured a language, :data:`DEFAULT_LANGUAGE`
    is used.
    """

    def __init__(self, language: Optional[str] = None) -> None:
        self._language = language or DEFAULT_LANGUAGE

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str) -> None:
        if language not in SUPPORTED_LANGUAGES:
            return
        self._language = language

    def t(self, key: str, **kwargs: Any) -> str:
        """Translate ``key``.

        Falls back to the FALLBACK_LANGUAGE and finally to the key
        itself if a translation is missing — so we always return
        something readable.
        """
        with _LOCK:
            primary = _load(self._language)
            value = _lookup(primary, key)
            if value is None and self._language != FALLBACK_LANGUAGE:
                fallback = _load(FALLBACK_LANGUAGE)
                value = _lookup(fallback, key)
            if value is None:
                value = key
        try:
            return value.format(**kwargs)
        except Exception:
            return value

    # Convenience alias so callers can write ``_("foo.bar")``.
    def __call__(self, key: str, **kwargs: Any) -> str:
        return self.t(key, **kwargs)


_default = Translator()


def set_default_language(language: str) -> None:
    _default.set_language(language)


def get_default_language() -> str:
    return _default.language


def t(key: str, **kwargs: Any) -> str:
    return _default.t(key, **kwargs)


def _(key: str, **kwargs: Any) -> str:
    return _default.t(key, **kwargs)


def list_languages() -> Iterable[tuple[str, str]]:
    return list(SUPPORTED_LANGUAGES.items())


def resolve_language(preferred: Optional[str]) -> str:
    """Validate ``preferred`` and return a supported language code."""
    if preferred and preferred in SUPPORTED_LANGUAGES:
        return preferred
    return DEFAULT_LANGUAGE
