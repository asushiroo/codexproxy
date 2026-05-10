from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from collections.abc import Mapping

DEBUG_RECORD_DIR = Path("/tmp/codexproxy-records")
TERMINAL_WORD_LIMIT = 500


def build_debug_record(
    *,
    client_name: str,
    port: int,
    downstream_request: dict,
    upstream_request: dict,
    upstream_response: dict | None = None,
    upstream_error: dict | None = None,
) -> dict:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "client_name": client_name,
        "port": port,
        "downstream_request": downstream_request,
        "upstream_request": upstream_request,
        "upstream_response": upstream_response,
        "upstream_error": upstream_error,
    }


def save_debug_record(record: dict, directory: Path | None = None) -> Path:
    directory = directory or DEBUG_RECORD_DIR
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"{timestamp}-{record['client_name']}-{uuid4().hex[:8]}.json"
    path = directory / filename
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def format_debug_record_summary(record: dict, *, file_path: Path, word_limit: int = TERMINAL_WORD_LIMIT) -> str:
    summary = {
        "file": str(file_path),
        "record": _truncate_value(record, word_limit=word_limit),
    }
    return "RECORD " + json.dumps(summary, ensure_ascii=False, indent=2)


def build_http_message_snapshot(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    status: int | None = None,
    reason: str | None = None,
) -> dict:
    snapshot = {
        "method": method,
        "url": url,
        "headers": dict(headers.items()),
        "body": _render_body_value(body, headers),
    }
    if status is not None:
        snapshot["status"] = status
    if reason is not None:
        snapshot["reason"] = reason
    return snapshot


def _render_body_value(body: bytes, headers: Mapping[str, str]):
    if not body:
        return ""

    content_encoding = headers.get("Content-Encoding")
    if content_encoding and content_encoding.lower() != "identity":
        return {
            "omitted": True,
            "reason": "encoded-body",
            "content_encoding": content_encoding,
            "bytes": len(body),
        }

    content_type = _extract_content_type(headers)
    if content_type and not _is_text_content_type(content_type):
        return {
            "omitted": True,
            "reason": "non-text-body",
            "content_type": content_type,
            "bytes": len(body),
        }

    text = _decode_text_body(body, headers)
    if text is None:
        return {
            "omitted": True,
            "reason": "binary-body",
            "bytes": len(body),
        }

    if content_type and _is_json_content_type(content_type):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return text


def _truncate_value(value, *, word_limit: int):
    if isinstance(value, str):
        return _truncate_words(value, word_limit=word_limit)
    if isinstance(value, list):
        return [_truncate_value(item, word_limit=word_limit) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_value(item, word_limit=word_limit) for key, item in value.items()}
    return value


def _truncate_words(text: str, *, word_limit: int) -> str:
    words = text.split()
    if len(words) <= word_limit:
        return text
    return " ".join(words[:word_limit]) + f" ...(truncated {len(words) - word_limit} words)"


def _extract_content_type(headers: Mapping[str, str]) -> str | None:
    header_value = headers.get("Content-Type", "")
    if not header_value:
        return None
    return header_value.split(";", 1)[0].strip().lower() or None


def _extract_declared_charset(headers: Mapping[str, str]) -> str | None:
    header_value = headers.get("Content-Type", "")
    for segment in header_value.split(";")[1:]:
        key, separator, value = segment.strip().partition("=")
        if separator and key.lower() == "charset" and value:
            return value.strip().strip('"')
    return None


def _decode_text_body(body: bytes, headers: Mapping[str, str]) -> str | None:
    declared_charset = _extract_declared_charset(headers)
    candidate_charsets = [charset for charset in [declared_charset, "utf-8"] if charset]
    if declared_charset is None or declared_charset.lower() == "utf-8":
        candidate_charsets.append("gb18030")

    for charset in candidate_charsets:
        try:
            return body.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue

    return None


def _is_json_content_type(content_type: str) -> bool:
    return content_type == "application/json" or content_type.endswith("+json")


def _is_text_content_type(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or _is_json_content_type(content_type)
        or content_type in {"application/xml", "application/x-ndjson"}
    )
