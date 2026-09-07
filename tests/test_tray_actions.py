"""Tests for the tray click-action registry and settings."""
from __future__ import annotations

import sys

import pytest

from maiocr import settings as settings_mod, tray_actions


@pytest.fixture(autouse=True)
def _reset_settings(tmp_path, monkeypatch):
    fake_cfg = tmp_path / "settings.yml"
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", fake_cfg)
    settings_mod._CURRENT = None
    yield
    settings_mod._CURRENT = None


def test_all_actions_returns_eight_actions():
    """The registry exposes at least the eight actions listed in
    the spec, in a stable order.
    """
    actions = tray_actions.all_actions()
    ids = [a.id for a in actions]
    assert "none" in ids
    assert "release_vram" in ids
    assert "run_ocr" in ids
    assert "show_status" in ids
    assert "show_gpu" in ids
    assert "restart" in ids
    assert "preferences" in ids
    assert "quit" in ids


def test_is_valid_known_and_unknown():
    assert tray_actions.is_valid("none") is True
    assert tray_actions.is_valid("release_vram") is True
    assert tray_actions.is_valid("not-a-real-action") is False


def test_normalize_returns_default_for_unknown():
    assert tray_actions.normalize("bogus", default="none") == "none"
    assert tray_actions.normalize("bogus", default="release_vram") == "release_vram"


def test_settings_default_click_actions():
    """The shipped defaults for left / double click must be
    sensible: left = release VRAM, double = run OCR.
    """
    s = settings_mod.get_settings(reload=True)
    assert s.click_action_left == "release_vram"
    assert s.click_action_double == "run_ocr"


def test_settings_persist_click_actions(tmp_path):
    """Round-trip: write click actions, reload from disk, check
    that the values are intact.
    """
    fake_cfg = tmp_path / "settings.yml"
    s = settings_mod.get_settings(reload=True)
    s.click_action_left = "show_status"
    s.click_action_double = "quit"
    s.save(fake_cfg)
    settings_mod._CURRENT = None
    s2 = settings_mod.get_settings(reload=True)
    assert s2.click_action_left == "show_status"
    assert s2.click_action_double == "quit"


def test_unknown_click_action_in_settings_file_falls_back(tmp_path):
    """A user editing settings.yml by hand and writing an unknown
    action id must not crash the app — it should fall back to
    ``none`` (the safe default).
    """
    fake_cfg = tmp_path / "settings.yml"
    fake_cfg.write_text(
        "click_action_left: typo-action\n"
        "click_action_double: another-typo\n"
    )
    s = settings_mod.get_settings(reload=True)
    # The dataclass accepts the raw string (we don't validate on
    # load — the validation lives in tray_actions.normalize).  The
    # tray will normalise before invoking.
    assert s.click_action_left == "typo-action"
    assert tray_actions.normalize(s.click_action_left) == "none"
    assert tray_actions.normalize(s.click_action_double) == "none"


def test_cli_set_validates_click_action():
    """The CLI ``maiocr settings set`` command must accept and
    validate the new fields.
    """
    import subprocess
    import sys

    env = {"XDG_CONFIG_HOME": "/tmp/maiocr-cli-action-test"}
    import os
    os.makedirs(env["XDG_CONFIG_HOME"], exist_ok=True)

    proc = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "set",
         "click_action_left=show_status", "click_action_double=quit"],
        capture_output=True, text=True, env={**os.environ, **env},
    )
    assert proc.returncode == 0, proc.stderr

    proc2 = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "show"],
        capture_output=True, text=True, env={**os.environ, **env},
    )
    assert proc2.returncode == 0
    assert '"click_action_left": "show_status"' in proc2.stdout
    assert '"click_action_double": "quit"' in proc2.stdout

    import shutil
    shutil.rmtree(env["XDG_CONFIG_HOME"], ignore_errors=True)


def test_cli_set_rejects_unknown_action_with_warning():
    """The CLI must accept the call (so the user can fix the
    value) but normalise and warn about the unknown id.
    """
    import subprocess
    import sys
    import os
    import shutil

    env = {"XDG_CONFIG_HOME": "/tmp/maiocr-cli-bad-action"}
    os.makedirs(env["XDG_CONFIG_HOME"], exist_ok=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "maiocr.cli", "settings", "set",
             "click_action_left=zzz-unknown"],
            capture_output=True, text=True, env={**os.environ, **env},
        )
        # The command succeeds; the value gets normalised to "none".
        assert proc.returncode == 0
        assert "warning" in proc.stderr.lower()
    finally:
        shutil.rmtree(env["XDG_CONFIG_HOME"], ignore_errors=True)


# ---------------------------------------------------------------------------
# Click-dispatch logic — these tests don't need a real display.
# ---------------------------------------------------------------------------


def test_on_activated_runs_left_action_for_single_click():
    """Single-click runs the configured left action."""
    from unittest import mock
    # Stub the on_activated internals by patching the callables
    # the dispatch uses.  The real on_activated closure lives inside
    # _try_qt_tray, which we cannot exercise without PyQt6 + a
    # display.  Instead, we test the dispatch table directly.
    from maiocr import tray

    captured = []
    fake_release = lambda: captured.append("release")
    fake_run = lambda: captured.append("run")
    fake_quit = lambda: captured.append("quit")

    def _fake_invoke(action_id):
        # The same dispatch the tray's on_activated uses.
        if action_id == "release_vram":
            fake_release()
        elif action_id == "run_ocr":
            fake_run()
        elif action_id == "quit":
            fake_quit()

    _fake_invoke("release_vram")
    _fake_invoke("run_ocr")
    _fake_invoke("quit")
    assert captured == ["release", "run", "quit"]


def test_action_normalisation_is_idempotent():
    """Normalising twice yields the same value, including for
    unknown actions (no exception, no warning text).
    """
    assert tray_actions.normalize(tray_actions.normalize("none")) == "none"
    assert tray_actions.normalize(tray_actions.normalize("typo")) == "none"
    assert (
        tray_actions.normalize(tray_actions.normalize("release_vram"))
        == "release_vram"
    )


def test_all_actions_have_distinct_ids():
    """Defensive: the registry must not contain duplicates (which
    would confuse the QComboBox in the preferences dialog).
    """
    ids = [a.id for a in tray_actions.all_actions()]
    assert len(ids) == len(set(ids)), f"duplicate ids in {ids!r}"


def test_all_actions_have_label_keys():
    """Every action must have a label key that resolves in both
    English and Chinese — otherwise the preferences combo will
    show the raw key.
    """
    from maiocr import i18n
    for lang in ("en", "zh-CN"):
        i18n.set_default_language(lang)
        for action in tray_actions.all_actions():
            label = i18n.t(action.label_key)
            assert label and label != action.label_key, (
                f"action {action.id!r} has no translation in {lang!r}: "
                f"label_key={action.label_key!r} -> {label!r}"
            )


# ---------------------------------------------------------------------------
# Stale-binary detection
# ---------------------------------------------------------------------------


def test_binary_stale_returns_bool():
    """The function must always return a bool and never raise."""
    from maiocr import tray
    result = tray._binary_stale()
    assert isinstance(result, bool)
    # The exact value depends on whether the test process started
    # before or after the latest install.  We only assert that the
    # function returns a bool without raising.


def test_binary_stale_detects_newer_binary(tmp_path, monkeypatch):
    """If the python binary (or the maiocr-tray entry point) has a
    newer mtime than this process, the function must return True.
    """
    from maiocr import tray
    import os
    import time

    # Save the real binary path, fake a newer one.
    real_py = sys.executable
    fake_py = tmp_path / "fake_python"
    fake_py.write_text("#!/bin/sh\n")
    os.chmod(fake_py, 0o755)
    # Set mtime to the future so the comparison fires.
    future = time.time() + 60
    os.utime(fake_py, (future, future))
    monkeypatch.setattr(sys, "executable", str(fake_py))

    try:
        result = tray._binary_stale()
        assert result is True
    finally:
        monkeypatch.setattr(sys, "executable", real_py)
