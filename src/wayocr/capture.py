import subprocess
import tempfile
from pathlib import Path


def capture() -> str:
    temp_file = tempfile.NamedTemporaryFile(
        suffix=".png",
        prefix="wayocr_",
        delete=False,
    )

    image_path = Path(temp_file.name)

    temp_file.close()

    geometry = subprocess.check_output(
        ["slurp"],
        text=True
    ).strip()

    subprocess.run(
        [
            "grim",
            "-g",
            geometry,
            str(image_path),
        ],
        check=True,
    )

    return str(image_path)