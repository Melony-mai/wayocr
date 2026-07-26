import subprocess


def notify(message: str):
    subprocess.run(
        [
            "notify-send",
            "WayOCR",
            message,
        ],
        check=False,
    )