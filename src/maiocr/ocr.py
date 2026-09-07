"""OCR engine wrapper.

The RapidOCR engine is heavy: loading models, attaching ONNX Runtime
sessions, and (with onnxruntime-gpu) allocating VRAM.

To meet the RAM/VRAM management requirements we:

* build the engine lazily on the first `recognize()` call,
* prefer the GPU provider when the system exposes one,
* expose `release_vram()` which drops the engine entirely so the next
  call re-initialises it.

The previous implementation built the engine in `OCR.__init__`, which
meant the model was resident from server start-up. With the new
behaviour the service itself stays in RAM (cheap) and the model is
loaded into VRAM only when an OCR request actually arrives.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from maiocr import gpu


@dataclass
class TextBox:
    """A single recognised text box in image coordinates."""

    text: str
    score: float
    # axis-aligned bounding box: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
    box: List[List[float]]

    @property
    def x(self) -> float:
        return float(min(p[0] for p in self.box))

    @property
    def y(self) -> float:
        return float(min(p[1] for p in self.box))

    @property
    def x2(self) -> float:
        return float(max(p[0] for p in self.box))

    @property
    def y2(self) -> float:
        return float(max(p[1] for p in self.box))

    @property
    def width(self) -> float:
        return self.x2 - self.x

    @property
    def height(self) -> float:
        return self.y2 - self.y

    @property
    def cx(self) -> float:
        return (self.x + self.x2) / 2.0

    @property
    def cy(self) -> float:
        return (self.y + self.y2) / 2.0


@dataclass
class OcrResult:
    boxes: List[TextBox]

    def to_list(self) -> List[dict]:
        return [
            {
                "text": b.text,
                "score": b.score,
                "box": b.box,
                "x": b.x,
                "y": b.y,
                "w": b.width,
                "h": b.height,
            }
            for b in self.boxes
        ]


class OCR:
    """Thread-safe wrapper around RapidOCR with lazy initialisation."""

    def __init__(self, prefer_gpu: bool = True) -> None:
        self._prefer_gpu = prefer_gpu
        self._engine = None
        self._provider = "CPUExecutionProvider"
        self._lock = threading.Lock()
        self._info: Optional[gpu.GpuInfo] = None

    @property
    def info(self) -> gpu.GpuInfo:
        if self._info is None:
            self._info = gpu.detect()
        return self._info

    @property
    def provider(self) -> str:
        if self._engine is None:
            return "unloaded"
        return self._provider

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def _build_engine(self) -> None:
        from rapidocr import RapidOCR

        info = self.info
        if self._prefer_gpu and info.available:
            try:
                self._engine = RapidOCR(
                    params={
                        "Global.log_level": "warning",
                        "EngineConfig.onnxruntime.use_cuda": True,
                    },
                )
                self._provider = info.provider
                return
            except Exception as exc:  # pragma: no cover - depends on driver
                print(
                    f"[maiocr] GPU engine init failed ({exc}); falling back to CPU",
                    flush=True,
                )
                self._engine = None

        self._engine = RapidOCR(
            params={"Global.log_level": "warning"},
        )
        self._provider = "CPUExecutionProvider"

    def _ensure_loaded(self) -> None:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    self._build_engine()

    def release_vram(self) -> bool:
        """Drop the OCR engine entirely, freeing VRAM and CPU caches.

        Returns True if anything was actually released.
        """
        with self._lock:
            if self._engine is None:
                return False
            engine = self._engine
            self._engine = None
            self._provider = "unloaded"
            try:
                for attr in ("text_det", "text_cls", "text_rec"):
                    sess = getattr(engine, attr, None)
                    if sess is None:
                        continue
                    inner = getattr(sess, "session", sess)
                    close = getattr(inner, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
            except Exception as exc:  # pragma: no cover
                print(f"[maiocr] engine close error: {exc}", flush=True)

        try:
            import gc
            gc.collect()
        except Exception:
            pass

        try:
            import onnxruntime as ort  # noqa: WPS433
            cuda = "CUDAExecutionProvider" in ort.get_available_providers()
            if cuda:
                try:
                    from onnxruntime.capi._pybind_state import (
                        OrtDevice,
                        get_available_providers,  # noqa: F401
                    )
                except Exception:
                    pass
        except Exception:
            pass

        return True

    def recognize(self, image: np.ndarray) -> OcrResult:
        """Run OCR on a BGR or grayscale numpy array."""
        self._ensure_loaded()
        assert self._engine is not None

        start = time.perf_counter()
        # RapidOCR accepts a numpy array directly; it returns a
        # RapidOCROutput object (or a 2-tuple on older versions).
        raw = self._engine(image)
        elapsed = time.perf_counter() - start

        boxes_list = self._extract_boxes(raw)

        boxes: List[TextBox] = []
        for box_points, text, score in boxes_list:
            boxes.append(
                TextBox(
                    text=str(text) if text is not None else "",
                    score=float(score) if score is not None else 0.0,
                    box=[[float(x), float(y)] for x, y in box_points],
                )
            )

        if elapsed < 0:
            return OcrResult([])
        return OcrResult(boxes)

    @staticmethod
    def _extract_boxes(raw) -> list:
        """Pull (box, text, score) tuples from any RapidOCR return shape."""
        # New API: RapidOCROutput dataclass
        boxes = getattr(raw, "boxes", None)
        txts = getattr(raw, "txts", None)
        scores = getattr(raw, "scores", None)
        if boxes is not None and txts is not None:
            n = len(txts)
            out = []
            for i in range(n):
                box = boxes[i]
                text = txts[i] if i < len(txts or []) else ""
                score = scores[i] if scores is not None and i < len(scores) else 0.0
                out.append((box, text, score))
            return out

        # Old API: list of (box, (text, score)) tuples
        if isinstance(raw, (list, tuple)) and raw and not hasattr(raw, "boxes"):
            out = []
            for item in raw:
                try:
                    box_points, (text, score) = item
                except Exception:
                    if len(item) >= 2 and isinstance(item[1], (list, tuple)):
                        box_points, text_score = item[0], item[1]
                        if isinstance(text_score, (list, tuple)) and len(text_score) == 2:
                            text, score = text_score
                        else:
                            text, score = text_score, 0.0
                    else:
                        continue
                out.append((box_points, text, score))
            return out

        return []
