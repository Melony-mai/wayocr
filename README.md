# WayOCR

WayOCR is a screenshot OCR tool designed for Linux Wayland desktop
environments.

It provides fast screenshot-based text recognition using:

-   RapidOCR
-   ONNX Runtime
-   Persistent OCR server
-   Unix socket communication
-   Wayland clipboard integration

Designed primarily for:

-   niri compositor
-   DankMaterialShell (DMS)
-   Chinese / Japanese / English mixed text recognition

## Features

-   Screenshot area OCR
-   Super + PrintScreen shortcut integration
-   Persistent OCR model server
-   No repeated model loading
-   Clipboard auto-copy
-   Desktop notification
-   systemd user service integration
-   Mixed language recognition

## Architecture

    Keyboard Shortcut
            |
            v
    niri / DMS
            |
            v
    wayocr client
            |
            v
    Unix Socket
            |
            v
    wayocr-server
            |
            v
    RapidOCR
            |
            v
    Text Processing
            |
            v
    Wayland Clipboard

## Requirements

-   Linux
-   Wayland session
-   Python \>= 3.12

Recommended environment:

-   Arch Linux
-   niri compositor
-   DankMaterialShell

Required tools:

``` bash
which wl-copy
which grim
which slurp
```

Install on Arch Linux:

``` bash
sudo pacman -S wl-clipboard grim slurp
```

## Installation

Clone repository:

``` bash
git clone <your-repository-url>
cd wayocr
```

Install dependencies:

``` bash
uv sync
```

Install command line tools:

``` bash
uv tool install .
```

Check:

``` bash
which wayocr
```

## OCR Server

WayOCR uses a persistent OCR server. The model is loaded once and
reused.

Enable systemd user service:

``` bash
systemctl --user daemon-reload
systemctl --user enable --now wayocr-server
```

Check status:

``` bash
systemctl --user status wayocr-server
```

## Usage

Press:

    Super + PrintScreen

Select an area.

WayOCR will:

1.  Capture screenshot
2.  Preprocess image
3.  Send request to OCR server
4.  Recognize text
5.  Clean result
6.  Copy text to clipboard
7.  Show notification

The result can also be accessed through DMS clipboard history.

## Commands

OCR screenshot:

``` bash
wayocr
```

Start server:

``` bash
wayocr-server
```

Check server:

``` bash
wayocr-status
```

Example:

    WayOCR status
    Server: running
    Socket: OK

## Project Structure

    wayocr
    ├── src
    │   └── wayocr
    │       ├── main.py
    │       ├── server.py
    │       ├── client.py
    │       ├── ocr.py
    │       ├── capture.py
    │       ├── preprocess.py
    │       ├── processor.py
    │       ├── clipboard.py
    │       ├── notify.py
    │       └── status.py
    ├── pyproject.toml
    ├── uv.lock
    ├── README.md
    └── .gitignore

## Performance

Current CPU mode:

    preprocess:
    ~10-50 ms

    OCR:
    ~1.9 seconds

    total:
    ~2 seconds

The persistent server avoids model loading delay.

## Models

Current models:

    PP-OCRv6_det_small.onnx
    PP-OCRv6_rec_small.onnx
    ch_ppocr_mobile_v2.0_cls_mobile.onnx

Runtime:

    ONNX Runtime

GPU acceleration can be added later with ONNX Runtime CUDA provider.

## Troubleshooting

Restart service:

``` bash
systemctl --user restart wayocr-server
```

View logs:

``` bash
journalctl --user -u wayocr-server -f
```

Check clipboard:

``` bash
which wl-copy
```

## License

MIT License
