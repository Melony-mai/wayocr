"""Tests for the cleanup module: verifies that only wayocr artefacts
are removed and unrelated paths are left alone.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_cleanup_removes_old_artifacts(tmp_path, monkeypatch):
    # Lay down fake artefacts inside a temp HOME.
    fake_home = tmp_path
    (fake_home / ".config" / "systemd" / "user").mkdir(parents=True)
    (fake_home / ".config" / "systemd" / "user" / "default.target.wants").mkdir(parents=True)
    unit = fake_home / ".config" / "systemd" / "user" / "wayocr-server.service"
    unit.write_text("[Unit]\nDescription=old\n")
    wants = (
        fake_home / ".config" / "systemd" / "user" / "default.target.wants"
        / "wayocr-server.service"
    )
    wants.symlink_to(unit)
    tool = fake_home / ".local" / "share" / "uv" / "tools" / "wayocr"
    tool.mkdir(parents=True)
    (tool / "marker").write_text("x")
    binlink = fake_home / ".local" / "bin" / "wayocr-server"
    binlink.parent.mkdir(parents=True)
    # Symlink must point to OLD_TOOL_DIR for cleanup to remove it
    binlink.symlink_to(tool / "wayocr-server")

    # Compat shim from new maiocr package: must NOT be removed
    new_tool = fake_home / ".local" / "share" / "uv" / "tools" / "maiocr"
    new_tool.mkdir(parents=True)
    compat_link = fake_home / ".local" / "bin" / "wayocr"
    compat_link.symlink_to(new_tool / "wayocr")

    # Unrelated artefact must NOT be touched.
    unrelated = fake_home / "unrelated.txt"
    unrelated.write_text("keep me")

    # Patch the cleanup module's hard-coded paths.
    from maiocr import cleanup as cleanup_mod

    monkeypatch.setattr(cleanup_mod, "OLD_UNIT", unit)
    monkeypatch.setattr(cleanup_mod, "OLD_WANTS", unit.parent / "default.target.wants")
    monkeypatch.setattr(cleanup_mod, "OLD_BIN_NAMES", ("wayocr", "wayocr-server", "wayocr-status"))
    monkeypatch.setattr(cleanup_mod, "OLD_TOOL_DIR", tool)
    monkeypatch.setattr(cleanup_mod, "OLD_SOCKETS", [])

    # Avoid running systemctl.
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    rc = cleanup_mod.cleanup(dry_run=False)
    assert rc == 0
    assert not unit.exists()
    assert not wants.exists()
    assert not binlink.exists()
    assert not tool.exists()
    # Compat shim should be left alone
    assert compat_link.is_symlink()
    assert unrelated.exists()


def test_cleanup_dry_run_does_not_delete(tmp_path, monkeypatch):
    fake_home = tmp_path
    unit = fake_home / "unit.service"
    unit.write_text("x")
    from maiocr import cleanup as cleanup_mod

    monkeypatch.setattr(cleanup_mod, "OLD_UNIT", unit)
    monkeypatch.setattr(cleanup_mod, "OLD_WANTS", unit.parent)
    monkeypatch.setattr(cleanup_mod, "OLD_BIN_NAMES", ())
    monkeypatch.setattr(cleanup_mod, "OLD_TOOL_DIR", fake_home / "notool")
    monkeypatch.setattr(cleanup_mod, "OLD_SOCKETS", [])

    import shutil
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    cleanup_mod.cleanup(dry_run=True)
    assert unit.exists()
