"""User-level settings for MaiOCR.

Persisted as YAML at ``$XDG_CONFIG_HOME/maiocr/settings.yml`` (falls
back to ``~/.config/maiocr/settings.yml``).

Settings currently include:

* ``language``         — UI language (``en`` or ``zh-CN``).
* ``auto_release_vram`` — automatically release the OCR engine after
                          every OCR call (default: ``false``).
* ``auto_release_seconds`` — if the engine is unused for this long,
                             release it (0 = disabled).
* ``log_level``        — one of ``debug``, ``info``, ``warning``,
                          ``error``.
* ``prefer_gpu``       — prefer CUDA provider when available.
* ``notify_on_copy``   — show desktop notification after a successful
                          OCR copy.

The class is intentionally simple: a typed dataclass and a tiny
``load``/``save`` pair.  Every read happens through the
``get_settings()`` singleton so that the i18n module, the tray and
the server all see the same values.
"""
from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from maiocr import i18n


CONFIG_DIR: Path = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "maiocr"
CONFIG_FILE: Path = CONFIG_DIR / "settings.yml"


@dataclass
class Settings:
    language: str = i18n.DEFAULT_LANGUAGE
    auto_release_vram: bool = False
    auto_release_seconds: int = 0
    log_level: str = "info"
    log_max_bytes: int = 1 * 1024 * 1024
    log_backup_count: int = 3
    log_retention_days: int = 7
    prefer_gpu: bool = True
    notify_on_copy: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        if path is None:
            path = CONFIG_FILE
        if not path.exists():
            inst = cls()
            inst._save_unchecked(path)
            return inst
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in raw.items() if k in known}
        inst = cls(**clean)
        # Validate the language and reset if unsupported.
        if inst.language not in i18n.SUPPORTED_LANGUAGES:
            inst.language = i18n.DEFAULT_LANGUAGE
        # If any new defaults were added (e.g. log_max_bytes) and the
        # file did not mention them, persist the merged file so the
        # on-disk view stays in sync with the dataclass.
        defaults = {f.name: getattr(inst, f.name) for f in fields(cls)}
        if any(k not in raw for k in defaults):
            inst.save()
        return inst

    def _save_unchecked(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(asdict(self), fh, allow_unicode=True, sort_keys=False)

    def save(self, path: Path | None = None) -> None:
        if path is None:
            path = CONFIG_FILE
        self._save_unchecked(path)

    def update(self, **kwargs: Any) -> None:
        known = {f.name for f in fields(self)}
        for key, value in kwargs.items():
            if key in known:
                setattr(self, key, value)
        if self.language not in i18n.SUPPORTED_LANGUAGES:
            self.language = i18n.DEFAULT_LANGUAGE

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_LOCK = threading.RLock()
_CURRENT: Optional[Settings] = None


def get_settings(reload: bool = False) -> Settings:
    """Return the cached settings, loading from disk on first call."""
    global _CURRENT
    with _LOCK:
        if _CURRENT is None or reload:
            _CURRENT = Settings.load()
            i18n.set_default_language(_CURRENT.language)
    return _CURRENT


def save_settings(settings: Settings) -> None:
    global _CURRENT
    with _LOCK:
        _CURRENT = settings
        settings.save()
        i18n.set_default_language(settings.language)
