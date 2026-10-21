import re
import unicodedata

_TAG = re.compile(r"<[^>]+>")


def clean_title(title: str) -> str:
    text = _TAG.sub("", title or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("《", "").replace("》", "")
    return re.sub(r"\s+", "", text)


def titles_match(seed: str, found: str) -> bool:
    left = clean_title(seed)
    right = clean_title(found)
    if not left or not right:
        return False
    if left == right:
        return True
    prefix = "中华人民共和国"
    if left.startswith(prefix) and left[len(prefix) :] == right:
        return True
    if right.startswith(prefix) and right[len(prefix) :] == left:
        return True
    return False
