from wayocr.ocr import OCR


ocr = OCR()

text = ocr.recognize("test.png")

print(text)