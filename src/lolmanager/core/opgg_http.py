from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup


MAX_OPGG_RESPONSE_BYTES = 1_000_000
MAX_OPGG_CHAMPION_ROWS = 250
MAX_OPGG_COUNTER_LINK_SCAN = 100


def next_page_stream(soup: BeautifulSoup) -> str:
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    for script in soup.select("script:not([src])"):
        text = script.get_text()
        for match in re.finditer(r"self\.__next_f\.push\(", text):
            try:
                value, _ = decoder.raw_decode(text[match.end():].lstrip())
            except ValueError:
                continue
            if isinstance(value, list) and len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])
    return "".join(chunks)


def _content_length(headers: object) -> int | None:
    if not hasattr(headers, "get"):
        return None
    getter = getattr(headers, "get")
    raw = getter("Content-Length") or getter("content-length")
    try:
        return int(str(raw).strip()) if raw is not None else None
    except ValueError:
        return None


def read_limited_text_response(
    response: Any,
    *,
    max_bytes: int = MAX_OPGG_RESPONSE_BYTES,
) -> str:
    content_length = _content_length(getattr(response, "headers", None))
    if content_length is not None and content_length > max_bytes:
        raise ValueError("OP.GG response too large")

    try:
        iter_content = getattr(response, "iter_content", None)
        if callable(iter_content):
            chunks: list[bytes] = []
            total = 0
            for chunk in iter_content(chunk_size=65536):
                if not chunk:
                    continue
                data = chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk)
                total += len(data)
                if total > max_bytes:
                    raise ValueError("OP.GG response too large")
                chunks.append(data)
            encoding = str(getattr(response, "encoding", "") or "utf-8")
            return b"".join(chunks).decode(encoding, errors="replace")

        text = str(getattr(response, "text", "") or "")
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError("OP.GG response too large")
        return text
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
