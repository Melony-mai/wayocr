from pathlib import Path

from wayocr.capture import capture
from wayocr.client import recognize
from wayocr.clipboard import copy
from wayocr.notify import notify
from wayocr.processor import clean_text


def main():
    image = None

    try:
        image = capture()

        # 调用常驻 OCR server
        text = recognize(image)

        # OCR结果清洗
        text = clean_text(text)

        if text:
            copy(text)
            notify("已复制到剪贴板")
        else:
            notify("没有检测到文字")

    except Exception as e:
        print(f"OCR错误: {e}")

        notify(
            f"OCR错误: {e}"
        )

    finally:
        if image:
            Path(image).unlink(
                missing_ok=True
            )


if __name__ == "__main__":
    main()