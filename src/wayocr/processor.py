import re


def clean_text(text: str) -> str:
    if not text:
        return ""

    # 统一换行
    text = text.replace("\r\n", "\n")

    # 去除每行首尾空格
    lines = [
        line.strip()
        for line in text.split("\n")
    ]

    # 删除连续空行
    result = []

    empty = False

    for line in lines:
        if line:
            result.append(line)
            empty = False
        else:
            if not empty:
                result.append("")
            empty = True

    return "\n".join(result).strip()