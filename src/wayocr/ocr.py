from rapidocr import RapidOCR


class OCR:
    def __init__(self):
        self.engine = RapidOCR()

    def recognize(self, image_path: str) -> str:
        result = self.engine(image_path)

        if not result.txts:
            return ""

        return "\n".join(result.txts)