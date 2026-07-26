from pathlib import Path

import cv2


def preprocess_image(image_path: str) -> str:

    path = Path(image_path)

    img = cv2.imread(
        str(path)
    )

    if img is None:
        raise RuntimeError(
            f"无法读取图片: {image_path}"
        )


    h, w = img.shape[:2]


    # 控制最大宽度
    max_width = 1600

    if w > max_width:
        scale = max_width / w

        img = cv2.resize(
            img,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA
        )

    # 小截图适当放大
    elif w < 800:
        scale = 1.5

        img = cv2.resize(
            img,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_CUBIC
        )


    gray = cv2.cvtColor(
        img,
        cv2.COLOR_BGR2GRAY
    )


    enhanced = cv2.convertScaleAbs(
        gray,
        alpha=1.2,
        beta=0
    )


    output = (
        path.parent
        / f"{path.stem}_processed.png"
    )


    cv2.imwrite(
        str(output),
        enhanced
    )


    return str(output)