"""`maiocr-status` command line: pretty-prints service status."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from maiocr import client, i18n, settings
from maiocr.paths import PID_FILE, SOCKET_PATH, STATUS_FILE


SERVICE = "MaiOCR.service"
SERVICE_FALLBACK = "maiocr.service"


def _systemctl_status() -> str:
    """Return a translated service-state string (without the label)."""
    if not shutil.which("systemctl"):
        return i18n.t("status.service_unknown")
    for name in (SERVICE, SERVICE_FALLBACK):
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-active", name],
                text=True,
                capture_output=True,
            )
        except Exception as exc:
            return f"{i18n.t('status.service_unknown')} ({exc})"
        if proc.returncode in (0, 3):
            out = (proc.stdout or proc.stderr or "").strip()
            if proc.returncode == 0 and not out:
                return i18n.t("status.service_active")
            if out == "active":
                return i18n.t("status.service_active")
            if out == "inactive":
                return i18n.t("status.service_inactive")
            return out or i18n.t("status.service_unknown")
    return i18n.t("status.service_inactive")


def main() -> int:
    s = settings.get_settings()
    i18n.set_default_language(s.language)
    print(i18n.t("app.name"))
    print("=" * max(8, len(i18n.t("app.name"))))
    print(f"{i18n.t('status.service_label')}: {_systemctl_status()}")

    sock_ok = SOCKET_PATH.exists()
    if sock_ok:
        print(i18n.t("status.socket_ok", path=SOCKET_PATH))
    else:
        print(i18n.t("status.socket_missing", path=SOCKET_PATH))

    pid = ""
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
    if pid:
        print(i18n.t("status.pid", pid=pid))

    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
            engine_state = i18n.t(
                "status.engine_loaded_value" if data.get("engine_loaded")
                else "status.engine_unloaded_value"
            )
            if data.get("engine_loaded"):
                provider_state = i18n.t(
                    "provider." + (data.get("provider") or "unloaded")
                )
                print(
                    f"{i18n.t('status.engine_label')}: {engine_state} ({provider_state})"
                )
            else:
                print(f"{i18n.t('status.engine_label')}: {engine_state}")
            print(
                i18n.t("status.gpu_available") if data.get("gpu_available")
                else i18n.t("status.gpu_unavailable")
            )
            if data.get("gpu_device"):
                print(i18n.t("status.gpu_device", device=data["gpu_device"]))
        except Exception as exc:
            print(f"status: unreadable ({exc})")
    else:
        print(i18n.t("status.engine_unloaded"))

    info = client.status()
    if info is not None:
        print(f"server: {info}")
    else:
        print(i18n.t("errors.server_unreachable"))

    return 0 if sock_ok else 1


if __name__ == "__main__":
    sys.exit(main())
