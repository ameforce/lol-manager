from __future__ import annotations


def normalize_name(value: object) -> str:
    return "".join(str(value or "").split()).casefold()


def is_plausible_champion_name(value: object) -> bool:
    """Reject non-name text before it reaches champion selection or its cache."""
    text = str(value or "").strip()
    if not text or not any(character.isalpha() for character in text):
        return False
    return all(
        character.isalpha()
        or character.isspace()
        or character in "'’&.-"
        for character in text
    )
