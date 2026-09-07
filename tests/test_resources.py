"""Tests for the resources module (icon discovery)."""
from __future__ import annotations

from pathlib import Path

import pytest

from maiocr import resources


def test_find_icon_in_package():
    # The package's own data/ dir always ships icon.ico
    path = resources.find_icon("icon.ico")
    assert path is not None
    assert path.is_file()
    assert path.name == "icon.ico"


def test_find_icon_missing_returns_none(tmp_path, monkeypatch):
    # Force a path with no icon
    monkeypatch.setattr(
        resources, "_candidate_roots", lambda: iter([tmp_path])
    )
    assert resources.find_icon("icon.ico") is None


def test_install_icon_copies_and_chmods(tmp_path):
    src = tmp_path / "src.ico"
    src.write_bytes(b"fake icon data")
    dest_dir = tmp_path / "dest"
    out = resources.install_icon(src, dest_dir)
    assert out.is_file()
    assert out.read_bytes() == b"fake icon data"
    assert oct(out.stat().st_mode & 0o777) == "0o644"


def test_real_icon_loads_as_qt_icon(tmp_path):
    """Smoke-test that the bundled icon is a real 256x256 image."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("PIL not available")
    p = resources.find_icon("icon.ico")
    assert p is not None
    img = Image.open(p)
    assert img.size[0] >= 64
    assert img.size[1] >= 64
    assert img.mode in ("RGBA", "RGB", "P")


def test_find_resource_for_nested_assets(tmp_path, monkeypatch):
    """If the data dir has a 'locales/en.yml' file, find_resource should
    return it."""
    fake = tmp_path / "locales"
    fake.mkdir()
    (fake / "en.yml").write_text("hello: world")
    monkeypatch.setattr(resources, "_candidate_roots", lambda: iter([tmp_path]))
    p = resources.find_resource("locales", "en.yml")
    assert p is not None
    assert p.read_text() == "hello: world"
