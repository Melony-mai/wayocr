"""Tests for the settings CLI: read/write, language switching, persistence."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_settings_lang_round_trip(tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "lang", "zh-CN"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    settings_file = tmp_path / "maiocr" / "settings.yml"
    assert settings_file.exists()
    assert "zh-CN" in settings_file.read_text()


def test_settings_set_persists(tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "set",
         "auto_release_vram=false", "log_level=warning"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    settings_file = tmp_path / "maiocr" / "settings.yml"
    data = settings_file.read_text()
    assert "auto_release_vram: false" in data
    assert "log_level: warning" in data
    # And a follow-up set should not reset it
    proc = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "set",
         "language=zh-CN"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    data = settings_file.read_text()
    assert "auto_release_vram: false" in data
    assert "log_level: warning" in data
    assert "language: zh-CN" in data


def test_settings_show_after_change(tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "set",
         "language=zh-CN"],
        check=True, capture_output=True, text=True, env=env,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "maiocr.cli", "settings", "show"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert '"language": "zh-CN"' in proc.stdout
