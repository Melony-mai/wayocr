import subprocess


def copy(text: str):
    subprocess.run(
        ["wl-copy"],
        input=text,
        text=True,
        check=True,
    )