from __future__ import annotations


def normalize_name(value: object) -> str:
    return "".join(str(value or "").split()).casefold()
