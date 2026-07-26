import json
import os
import socket
import time
from pathlib import Path

from wayocr.ocr import OCR
from wayocr.preprocess import preprocess_image


SOCKET_PATH = "/tmp/wayocr.sock"


def cleanup_socket():
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)


def handle_request(conn, ocr):
    processed_image = None

    try:
        data = conn.recv(4096)

        if not data:
            return

        request = json.loads(
            data.decode("utf-8")
        )

        image_path = request["image"]

        start_time = time.perf_counter()

        print(
            f"OCR request: {image_path}",
            flush=True
        )

        # 图片预处理计时
        preprocess_start = time.perf_counter()

        processed_image = preprocess_image(
            image_path
        )

        preprocess_time = (
            time.perf_counter()
            - preprocess_start
        )

        # OCR识别计时
        ocr_start = time.perf_counter()

        text = ocr.recognize(
            processed_image
        )

        ocr_time = (
            time.perf_counter()
            - ocr_start
        )

        total_time = (
            time.perf_counter()
            - start_time
        )

        print(
            f"OCR done: {len(text)} chars | "
            f"preprocess {preprocess_time:.3f}s | "
            f"ocr {ocr_time:.3f}s | "
            f"total {total_time:.3f}s",
            flush=True
        )

        response = {
            "text": text
        }

        conn.sendall(
            json.dumps(
                response,
                ensure_ascii=False
            ).encode("utf-8")
        )

    except Exception as e:
        print(
            f"OCR error: {e}",
            flush=True
        )

        response = {
            "error": str(e)
        }

        conn.sendall(
            json.dumps(
                response,
                ensure_ascii=False
            ).encode("utf-8")
        )

    finally:
        if processed_image:
            Path(
                processed_image
            ).unlink(
                missing_ok=True
            )

        conn.close()


def main():

    cleanup_socket()

    print(
        "Loading OCR model...",
        flush=True
    )

    # 模型只加载一次
    ocr = OCR()

    server = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    server.bind(
        SOCKET_PATH
    )

    server.listen(5)

    print(
        "WayOCR server running",
        flush=True
    )

    try:
        while True:
            conn, _ = server.accept()

            handle_request(
                conn,
                ocr
            )

    finally:
        server.close()
        cleanup_socket()


if __name__ == "__main__":
    main()