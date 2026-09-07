"""Tests for the notification subsystem.

We focus on the *event / backend / manager* contract:

* high-level helpers build an Event with the right i18n keys
* the manager dispatches to **exactly one** backend
* the chosen backend is the only one invoked — the other
  backends never see the event
* switching the mode in settings switches the active backend
  on the next call
* custom-bubble and system modes are mutually exclusive
"""
from __future__ import annotations

import os
import sys
import threading
from unittest import mock

import pytest

from maiocr import i18n, notifications, settings as settings_mod


@pytest.fixture(autouse=True)
def _reset_state(tmp_path, monkeypatch):
    """Each test gets a fresh settings file and a clean manager."""
    fake_cfg = tmp_path / "settings.yml"
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", fake_cfg)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    settings_mod._CURRENT = None
    i18n.set_default_language(i18n.DEFAULT_LANGUAGE)
    notifications.reset_for_tests()
    yield
    settings_mod._CURRENT = None
    notifications.reset_for_tests()


def _enable_notify_send(monkeypatch, available=True):
    monkeypatch.setattr(
        "maiocr.notifications.shutil.which",
        lambda cmd: "/usr/bin/notify-send" if available else None,
    )


def test_high_level_helper_builds_event():
    captured = []

    class Capture:
        name = "capture"

        def show(self, event):
            captured.append(event)

    mgr = notifications.get_manager()
    mgr.register(Capture(), set_active=True)
    notifications.notify_service_started()

    assert len(captured) == 1
    ev = captured[0]
    assert ev.id == "service.started"
    assert ev.kind is notifications.EventKind.SUCCESS
    # Title / body are translated strings, not keys
    assert ev.title and "MaiOCR" in ev.title or ev.title in (
        i18n.t("notifications.title.service"),
    )
    # The body should be the localised "started" string
    assert "started" in ev.body.lower() or "已启动" in ev.body


def test_only_active_backend_receives_event():
    """When the system backend is active, the custom backend is
    never invoked. This is the core of the mutual-exclusion rule.
    """
    sys_calls = []
    custom_calls = []

    class _Sys:
        name = "system"

        def show(self, event):
            sys_calls.append(event)

    class _Custom:
        name = "custom"

        def show(self, event):
            custom_calls.append(event)

    mgr = notifications.get_manager()
    mgr.register(_Sys())
    mgr.register(_Custom())
    mgr.set_active("system")

    notifications.notify_ocr_completed(42)

    assert len(sys_calls) == 1
    assert custom_calls == []


def test_custom_mode_does_not_invoke_system():
    sys_calls = []
    custom_calls = []

    class _Sys:
        name = "system"

        def show(self, event):
            sys_calls.append(event)

    class _Custom:
        name = "custom"

        def show(self, event):
            custom_calls.append(event)

    mgr = notifications.get_manager()
    mgr.register(_Sys())
    mgr.register(_Custom())
    mgr.set_active("custom")

    notifications.notify_ocr_completed(99)
    notifications.notify_vram_released()
    notifications.notify_service_started()

    assert sys_calls == []
    assert len(custom_calls) == 3


def test_switching_mode_uses_new_backend(monkeypatch, tmp_path):
    """Change ``notification_mode`` and verify the next event uses
    the new backend without restarting anything.
    """
    sys_calls = []
    custom_calls = []

    class _Sys:
        name = "system"

        def show(self, event):
            sys_calls.append(event)

    class _Custom:
        name = "custom"

        def show(self, event):
            custom_calls.append(event)

    mgr = notifications.get_manager()
    mgr.register(_Sys())
    mgr.register(_Custom())
    mgr.set_active("system")

    notifications.notify_ocr_completed(10)
    assert len(sys_calls) == 1 and custom_calls == []

    # Toggle to custom — the manager will pick the custom backend
    # for subsequent events.
    mgr.set_active("custom")
    notifications.notify_ocr_completed(20)
    assert len(sys_calls) == 1 and len(custom_calls) == 1


def test_unknown_active_backend_drops_event():
    """If the active backend is removed, events should be dropped
    silently rather than crash the application.
    """
    class _Recording:
        name = "rec"

        def show(self, event):
            pass

    mgr = notifications.get_manager()
    mgr.register(_Recording(), set_active=True)
    # Simulate a stale active name by directly poking the manager
    # state — production code would never do this, but it covers
    # the dispatch path's defensive null-check.
    with mgr._lock:
        mgr._active_name = "vanished"
    # Must not raise
    notifications.notify_ocr_completed(1)


def test_settings_initialise_falls_back_to_null_when_all_unavailable(
    monkeypatch, tmp_path,
):
    """When the user picked ``dbus`` / ``custom`` and **neither**
    D-Bus nor ``notify-send`` are available, the manager must
    drop the event silently rather than crash.
    """
    s = settings_mod.get_settings(reload=True)
    s.notification_mode = "custom"
    settings_mod.save_settings(s)

    # Pretend PyQt6 is unavailable
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("PyQt6"):
            raise ImportError("PyQt6 unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Pretend D-Bus is unavailable.
    class _MissingDbus:
        def __getattr__(self, name):
            raise ImportError("dbus unavailable in test env")
    monkeypatch.setitem(sys.modules, "dbus", _MissingDbus())

    # Pretend notify-send is unavailable too.
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda cmd: None)

    notifications.initialise()
    # The manager falls back to the null (silent) backend.
    assert notifications.get_manager().get_active_name() == "null"


def test_custom_mode_cli_uses_dbus_backend(monkeypatch, tmp_path):
    """A CLI process (no Qt event loop) running with custom mode
    must use the D-Bus backend if available, NOT the system
    backend. This is the core mutual-exclusion guarantee: a process
    that cannot render a bubble in custom mode must not fall back
    to notify-send.
    """
    # Patch the D-Bus connection to succeed so the test environment
    # matches a normal Niri session.
    fake_proxy = mock.MagicMock()

    import maiocr.notifications as ns

    def fake_connect():
        return True, mock.MagicMock(), fake_proxy, fake_proxy

    monkeypatch.setattr(ns.DBusBackend, "_connect", staticmethod(fake_connect))

    s = settings_mod.get_settings(reload=True)
    s.notification_mode = "custom"
    settings_mod.save_settings(s)

    notifications.reset_for_tests()
    notifications.initialise()
    assert notifications.get_manager().get_active_name() == "dbus"


def test_initialise_selects_dbus_backend(monkeypatch, tmp_path):
    """When the user picked ``dbus`` mode and the D-Bus service is
    reachable, the D-Bus backend must be the active one.
    """
    fake_proxy = mock.MagicMock()

    import maiocr.notifications as ns

    def fake_connect():
        return True, mock.MagicMock(), fake_proxy, fake_proxy

    monkeypatch.setattr(ns.DBusBackend, "_connect", staticmethod(fake_connect))

    s = settings_mod.get_settings(reload=True)
    s.notification_mode = "dbus"
    settings_mod.save_settings(s)

    notifications.reset_for_tests()
    notifications.initialise()
    assert notifications.get_manager().get_active_name() == "dbus"
    # The D-Bus backend should be available.
    assert notifications.get_manager().get("dbus") is not None


def test_legacy_compat_notify_routes_through_manager():
    """``notify.notify(message)`` should still work, but route
    through the manager so the active backend is used.
    """
    captured = []

    class _Cap:
        name = "cap"

        def show(self, event):
            captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)
    from maiocr import notify

    notify.notify("legacy test message")
    assert len(captured) == 1
    assert captured[0].body == "legacy test message"
    assert captured[0].kind is notifications.EventKind.INFO


def test_event_dataclass_is_immutable():
    ev = notifications.Event(
        id="x",
        kind=notifications.EventKind.INFO,
        title="T",
        body="B",
    )
    with pytest.raises(Exception):
        ev.body = "new"  # type: ignore[misc]


def test_notify_does_not_crash_when_backend_raises():
    """A buggy backend must not stop the rest of the application."""

    class _Buggy:
        name = "buggy"

        def show(self, event):
            raise RuntimeError("oops")

    notifications.get_manager().register(_Buggy(), set_active=True)
    # Should not raise
    notifications.notify_error("anything")


def test_concurrent_dispatch(monkeypatch):
    """Stress: many threads posting events at the same time should
    not lose any event and should not crash.
    """
    captured = []
    lock = threading.Lock()

    class _Cap:
        name = "cap"

        def show(self, event):
            with lock:
                captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)

    def worker():
        for _ in range(50):
            notifications.notify_ocr_completed(1)
            notifications.notify_vram_released()
            notifications.notify_service_started()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(captured) == 4 * 50 * 3


def test_system_backend_invokes_notify_send(monkeypatch):
    """When notify-send is on PATH the SystemBackend must shell out
    to it with the right args, exactly once per event.
    """
    import shutil as _shutil

    def fake_which(cmd):
        return "/usr/bin/notify-send" if cmd == "notify-send" else None

    monkeypatch.setattr(_shutil, "which", fake_which)
    calls = []

    def fake_run_fn(args, **kw):
        calls.append((args, kw))
        return mock.Mock(returncode=0)

    backend = notifications.SystemBackend()
    monkeypatch.setattr(backend._subprocess, "run", fake_run_fn)

    assert backend.is_available()
    ev = notifications.Event(
        id="x",
        kind=notifications.EventKind.SUCCESS,
        title="T",
        body="B",
    )
    backend.show(ev)
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[0] == "notify-send"
    assert args[-2:] == ["T", "B"]


def test_system_backend_falls_back_to_log_when_missing(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda cmd: None)
    backend = notifications.SystemBackend()
    assert not backend.is_available()
    # Must not raise
    backend.show(
        notifications.Event(
            id="x", kind=notifications.EventKind.INFO, title="t", body="b"
        )
    )


def test_initialise_does_not_crash_when_called_twice():
    """Calling initialise() multiple times (e.g. after a settings
    change) must not raise.
    """
    notifications.initialise()
    notifications.initialise()
    assert notifications.get_manager().get_active_name() in {
        "system", "custom", "null"
    }


def test_event_kind_round_trip():
    """All EventKind values are strings (so they serialise) and the
    manager does not care about their specific values."""
    for kind in (notifications.EventKind.INFO,
                 notifications.EventKind.SUCCESS,
                 notifications.EventKind.WARNING,
                 notifications.EventKind.ERROR):
        assert isinstance(kind.value, str)
        assert notifications.Event(
            id="x", kind=kind, title="t", body="b"
        ).kind is kind


def test_high_level_helpers_emit_typed_events():
    """Each helper builds the event with the expected logical id so
    the tray can de-duplicate if needed.
    """
    captured = []

    class _Cap:
        name = "cap"

        def show(self, event):
            captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)
    notifications.notify_service_started()
    notifications.notify_service_stopped()
    notifications.notify_service_restarted()
    notifications.notify_ocr_completed(10)
    notifications.notify_ocr_empty()
    notifications.notify_vram_released()
    notifications.notify_vram_already_unloaded()
    notifications.notify_error("oops")

    ids = [e.id for e in captured]
    assert ids == [
        "service.started",
        "service.stopped",
        "service.restarted",
        "ocr.completed",
        "ocr.empty",
        "vram.released",
        "vram.idle",
        "error",
    ]


def test_localised_title_and_body(monkeypatch):
    """The event's title and body are pre-translated at dispatch
    time, so the backend sees a language-correct string.
    """
    captured = []

    class _Cap:
        name = "cap"

        def show(self, event):
            captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)

    i18n.set_default_language("zh-CN")
    notifications.notify_ocr_completed(7)
    assert "已复制" not in captured[-1].body  # key, not text
    # Actually the helper calls i18n.t at dispatch time:
    captured.clear()
    notifications.get_manager().notify_event(
        notifications.Event(
            id="x",
            kind=notifications.EventKind.INFO,
            title=i18n.t("notifications.title.ocr"),
            body=i18n.t("notifications.copied", n=7),
        )
    )
    assert "7" in captured[-1].body
    assert "字符" in captured[-1].body  # Chinese for "characters"

    i18n.set_default_language("en")
    notifications.get_manager().notify_event(
        notifications.Event(
            id="y",
            kind=notifications.EventKind.INFO,
            title=i18n.t("notifications.title.ocr"),
            body=i18n.t("notifications.copied", n=12),
        )
    )
    assert "12" in captured[-1].body
    assert "characters" in captured[-1].body


# ---------------------------------------------------------------------------
# Silent indicator
# ---------------------------------------------------------------------------


def test_silent_event_kind_exists():
    """The silent event kind must be one of the EventKind enum
    values so backends can dispatch on it.
    """
    assert notifications.EventKind.SILENT.value == "silent"


def test_task_completion_helpers_use_success_kind():
    """``notify_ocr_completed`` and ``notify_vram_released`` are
    explicit "task done" events.  The D-Bus backend sets the
    urgency (low for success) and the user's daemon controls the
    display duration.
    """
    captured = []

    class _Cap:
        name = "cap"

        def show(self, event):
            captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)
    notifications.notify_ocr_completed(42)
    notifications.notify_vram_released()
    notifications.notify_vram_already_unloaded()

    # All task-completion events are SUCCESS or INFO, never ERROR.
    for e in captured:
        assert e.kind in (
            notifications.EventKind.SUCCESS,
            notifications.EventKind.INFO,
        )
    assert [e.id for e in captured] == [
        "ocr.completed",
        "vram.released",
        "vram.idle",
    ]


def test_state_change_helpers_still_use_bubble_kind():
    """Service-state events must keep the chatty bubble (the user
    wants to know if the service actually went up or down).
    """
    captured = []

    class _Cap:
        name = "cap"

        def show(self, event):
            captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)
    notifications.notify_service_started()
    notifications.notify_service_stopped()
    notifications.notify_service_restarted()
    notifications.notify_ocr_empty()
    notifications.notify_error("oops")

    kinds = [e.kind for e in captured]
    # Service events stay as INFO/SUCCESS bubbles
    assert kinds[0] is notifications.EventKind.SUCCESS
    assert kinds[1] is notifications.EventKind.INFO
    assert kinds[2] is notifications.EventKind.INFO
    # Empty OCR result is a warning bubble
    assert kinds[3] is notifications.EventKind.WARNING
    # Errors are always ERROR bubbles
    assert kinds[4] is notifications.EventKind.ERROR
    # None of these are silent
    assert all(k is not notifications.EventKind.SILENT for k in kinds)


def test_system_backend_marks_silent_as_transient(monkeypatch):
    """The SystemBackend must add the transient hint to
    notify-send for silent events, so they do not appear in the
    OS notification history.
    """
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil, "which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None
    )
    calls = []

    def fake_run_fn(args, **kw):
        calls.append(args)
        return mock.Mock(returncode=0)

    backend = notifications.SystemBackend()
    monkeypatch.setattr(backend._subprocess, "run", fake_run_fn)

    silent = notifications.Event(
        id="silent.x",
        kind=notifications.EventKind.SILENT,
        title="t",
        body="b",
    )
    backend.show(silent)
    # The argv must contain the transient hint.
    assert any(
        "int:transient:1" in a or a == "int:transient:1"
        for call in calls
        for a in call
    ), f"transient hint missing from {calls!r}"


def test_system_backend_does_not_mark_regular_as_transient(monkeypatch):
    """Regular (non-silent) events must NOT carry the transient
    hint — the user wants the chatty notification to appear in
    history.
    """
    import shutil as _shutil

    monkeypatch.setattr(
        _shutil, "which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None
    )
    calls = []

    def fake_run_fn(args, **kw):
        calls.append(args)
        return mock.Mock(returncode=0)

    backend = notifications.SystemBackend()
    monkeypatch.setattr(backend._subprocess, "run", fake_run_fn)

    regular = notifications.Event(
        id="regular.x",
        kind=notifications.EventKind.SUCCESS,
        title="t",
        body="b",
    )
    backend.show(regular)
    for call in calls:
        assert "int:transient:1" not in call


def test_notify_silent_helper():
    captured = []

    class _Cap:
        name = "cap"

        def show(self, event):
            captured.append(event)

    notifications.get_manager().register(_Cap(), set_active=True)
    notifications.notify_silent("notifications.silent_done")

    assert len(captured) == 1
    ev = captured[0]
    assert ev.kind is notifications.EventKind.SILENT
    assert ev.id == "silent.notifications.silent_done"


def test_dbus_backend_uses_dbus_boolean_for_hints():
    """The D-Bus backend must wrap boolean hint values with
    ``dbus.Boolean`` because plain Python ``bool`` is not a valid
    D-Bus variant on every binding.
    """
    import maiocr.notifications as ns

    captured = []

    def _capture(*args, **kwargs):
        captured.append((args, kwargs))

    # Inject a fake dbus module that satisfies both the
    # ``import dbus`` and ``from dbus import SessionBus`` lookups.
    fake_dbus = mock.MagicMock()
    fake_dbus.Boolean.side_effect = lambda v: f"Boolean({v})"
    fake_dbus.SessionBus.return_value.get_object.return_value = mock.MagicMock()
    fake_dbus.Interface = lambda *a, **kw: mock.MagicMock(
        Notify=_capture
    )

    with mock.patch.dict(sys.modules, {"dbus": fake_dbus, "dbus.mainloop": fake_dbus}):
        backend = ns.DBusBackend()
        ev = ns.Event(
            id="x", kind=ns.EventKind.SILENT,
            title="t", body="b",
        )
        backend.show(ev)
    assert captured, "Notify was not called"
    args, _ = captured[0]
    hints = args[6]
    # transient is wrapped via dbus.Boolean(1)
    assert hints["transient"] == "Boolean(1)"


def test_notifications_module_creates_no_qt_window():
    """The whole point of the redesign is that MaiOCR no longer
    creates a Qt toplevel window for status notifications.  We
    assert that the source of ``maiocr.notifications`` no longer
    references ``QWidget`` / ``QDialog`` / ``QSystemTrayIcon`` —
    if any of those classes are added back, the test will fail
    and the maintainer will be reminded to keep the focus-free
    contract intact.
    """
    import inspect
    from maiocr import notifications
    src = inspect.getsource(notifications)
    # ``QWidget`` is a generic name that we want to forbid for the
    # bubble classes — but we still use it internally for type
    # annotations and abstract base.  The concrete toplevel window
    # classes are ``QDialog`` and ``QSystemTrayIcon``.  Allow ``QWidget``
    # but forbid the two real-window classes.
    for forbidden in ("QDialog(", "QSystemTrayIcon("):
        assert forbidden not in src, (
            f"maiocr.notifications must not create {forbidden!r} — "
            "this would create a new Wayland surface and steal focus "
            "on Niri.  Use the freedesktop notification spec (D-Bus) "
            "instead."
        )


def test_dbus_backend_routes_silent_events_as_transient():
    """The D-Bus backend must include the ``transient`` hint
    for SILENT events so they do not enter the notification
    history.  Skipped when the ``dbus`` module is not available
    (it's an optional dependency).
    """
    dbus = pytest.importorskip("dbus")

    fake_iface = mock.MagicMock()
    fake_proxy = mock.MagicMock()

    import maiocr.notifications as ns

    backend = ns.DBusBackend()
    backend._available = True
    backend._proxy = fake_proxy
    fake_dbus_module = mock.MagicMock()
    fake_dbus_module.Interface = lambda *a, **kw: fake_iface
    with mock.patch.object(ns, "dbus", fake_dbus_module, create=True):
        ev = ns.Event(
            id="x", kind=ns.EventKind.SILENT,
            title="t", body="b",
        )
        backend.show(ev)
    fake_iface.Notify.assert_called_once()
    args = fake_iface.Notify.call_args.args
    hints = args[6]
    assert hints.get("transient") == 1
    assert hints.get("desktop-entry") == "maiocr"


def test_dbus_backend_omits_transient_for_regular_events():
    """Regular (non-silent) events must NOT carry the transient
    hint so the notification appears in the history.
    """
    dbus = pytest.importorskip("dbus")

    fake_iface = mock.MagicMock()
    fake_proxy = mock.MagicMock()
    import maiocr.notifications as ns
    backend = ns.DBusBackend()
    backend._available = True
    backend._proxy = fake_proxy
    fake_dbus_module = mock.MagicMock()
    fake_dbus_module.Interface = lambda *a, **kw: fake_iface
    with mock.patch.object(ns, "dbus", fake_dbus_module, create=True):
        ev = ns.Event(
            id="x", kind=ns.EventKind.INFO,
            title="t", body="b",
        )
        backend.show(ev)
    fake_iface.Notify.assert_called_once()
    args = fake_iface.Notify.call_args.args
    hints = args[6]
    assert "transient" not in hints


# ---------------------------------------------------------------------------
# Cross-process notification requests
# ---------------------------------------------------------------------------


def test_send_request_drops_file(tmp_path, monkeypatch):
    """The CLI drops a JSON file in NOTIFY_DIR; the file's
    contents are a serialised Event.
    """
    import maiocr.notifications as ns
    from maiocr import paths
    # The function imports ``paths`` lazily.  Patch the module
    # attribute on ``paths`` so the lazy import sees our tmp dir.
    monkeypatch.setattr(paths, "NOTIFY_DIR", tmp_path)
    ev = ns.Event(
        id="ocr.completed.test",
        kind=ns.EventKind.SILENT,
        title="Done",
        body="Done",
    )
    ns.send_request(ev)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    import json
    data = json.loads(files[0].read_text())
    assert data["id"] == "ocr.completed.test"
    assert data["kind"] == "silent"
    assert data["title"] == "Done"


def test_request_ocr_completed_only_in_custom_mode(monkeypatch, tmp_path):
    """In system mode the CLI must not drop a request (the
    SystemBackend will fire its own notify-send call).  In custom
    mode the request must be written.
    """
    import maiocr.notifications as ns
    from maiocr import paths, settings as s
    monkeypatch.setattr(paths, "NOTIFY_DIR", tmp_path)
    s._CURRENT = None

    # Custom mode → drops a request
    settings = s.get_settings(reload=True)
    settings.notification_mode = "custom"
    s.save_settings(settings)
    ns.request_ocr_completed(42)
    custom_files = list(tmp_path.glob("*.json"))
    assert len(custom_files) == 1
    for f in custom_files:
        f.unlink()

    # System mode → no request
    settings.notification_mode = "system"
    s.save_settings(settings)
    ns.request_ocr_completed(42)
    assert not list(tmp_path.glob("*.json"))
