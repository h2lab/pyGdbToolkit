# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Read-only client for the local pyGdbKnowledge retrieval API."""

import asyncio
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote, urlsplit

import requests

from .contracts import JsonObject, JsonValue

_MAX_TOKEN_FILE_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_RESULT_BYTES = 24 * 1024
_DOCUMENT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class KnowledgeError(RuntimeError):
    """A knowledge API operation failed with a sanitized message."""


def read_knowledge_token(path: Path) -> str:
    """Read one UTF-8 read token from a regular file.

    Parameters
    ----------
    path : Path
        File containing the read-only bearer token.

    Returns
    -------
    str
        The trimmed token value.

    Raises
    ------
    KnowledgeError
        If the file cannot be safely read or does not contain one token line.
    """
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise KnowledgeError("--knowledge-rd-token must name a regular file")
            data = source.read(_MAX_TOKEN_FILE_BYTES + 1)
    except KnowledgeError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise KnowledgeError("cannot read --knowledge-rd-token") from None
    if len(data) > _MAX_TOKEN_FILE_BYTES:
        raise KnowledgeError("--knowledge-rd-token exceeds 16384 bytes")
    try:
        token = data.decode("utf-8")
    except UnicodeDecodeError:
        raise KnowledgeError("--knowledge-rd-token must contain UTF-8") from None
    if token.endswith("\n"):
        token = token[:-1].removesuffix("\r")
    if any(character in token for character in "\r\n\0"):
        raise KnowledgeError(
            "--knowledge-rd-token must contain one nonempty line without NUL characters"
        )
    token = token.strip()
    if not token or any(character in token for character in "\r\n\0"):
        raise KnowledgeError(
            "--knowledge-rd-token must contain one nonempty line without NUL characters"
        )
    return token


class KnowledgeClient:
    """Call the pyGdbKnowledge read API; document content remains untrusted data."""

    def __init__(self, base_url: str, read_token: str | None = None) -> None:
        """Validate and retain an endpoint and optional read-only token."""
        parsed = urlsplit(base_url)
        try:
            parsed.port
        except ValueError:
            raise ValueError("knowledge URL must contain a valid port") from None
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("knowledge URL must be an HTTP(S) base URL without credentials")
        if read_token is not None and (
            not read_token.strip() or any(character in read_token for character in "\r\n\0")
        ):
            raise ValueError("knowledge read token must be one nonempty line")
        self._base_url = base_url.rstrip("/")
        self._read_token = read_token

    async def execute(self, tool_name: str, arguments: JsonObject, timeout: float) -> JsonObject:
        """Run one validated read tool without blocking the event loop."""
        return await asyncio.to_thread(self._execute, tool_name, arguments, timeout)

    def _execute(self, tool_name: str, arguments: JsonObject, timeout: float) -> JsonObject:
        if tool_name == "knowledge_search":
            query = arguments["query"]
            filters: dict[str, JsonValue] = {}
            for name in ("manufacturer", "device_family", "device", "peripheral"):
                value = arguments.get(name)
                if value is not None:
                    filters[name] = value
            document_types = arguments.get("document_types")
            if document_types is not None:
                filters["document_type"] = document_types
            body: dict[str, JsonValue] = {"query": query, "filters": filters}
            if "limit" in arguments:
                body["limit"] = arguments["limit"]
            result = self._request("POST", "/search", timeout, body)
            if not isinstance(result, dict) or not isinstance(result.get("results"), list):
                raise KnowledgeError("knowledge service returned an invalid search response")
            return _bounded_result(result, "search")
        if tool_name == "knowledge_get_document":
            document_id = _validated_id(arguments["document_id"], "document_id")
            encoded_id = quote(document_id, safe="")
            limit = arguments.get("limit", 100)
            offset = arguments.get("offset", 0)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 100
                or isinstance(offset, bool)
                or not isinstance(offset, int)
                or not 0 <= offset <= 2**31 - 1
            ):
                raise KnowledgeError("invalid document section pagination")
            document = self._request("GET", f"/documents/{encoded_id}", timeout)
            sections = self._request(
                "GET", f"/documents/{encoded_id}/sections?limit={limit}&offset={offset}", timeout
            )
            if (
                not isinstance(document, dict)
                or not isinstance(sections, dict)
                or not isinstance(sections.get("sections"), list)
                or not isinstance(sections.get("has_more"), bool)
                or isinstance(sections.get("offset"), bool)
                or not isinstance(sections.get("offset"), int)
                or sections.get("offset") != offset
            ):
                raise KnowledgeError("knowledge service returned invalid document metadata")
            return _bounded_result(
                {"document": document, "table_of_contents": sections}, "document"
            )
        if tool_name == "knowledge_get_section":
            document_id = _validated_id(arguments["document_id"], "document_id")
            section_id = _validated_id(arguments["section_id"], "section_id")
            result = self._request(
                "GET",
                f"/documents/{quote(document_id, safe='')}/section/{quote(section_id, safe='')}",
                timeout,
            )
            if not isinstance(result, dict):
                raise KnowledgeError("knowledge service returned an invalid section response")
            return _bounded_result(result, "section")
        raise KnowledgeError(f"unknown knowledge tool: {tool_name}")

    def _request(
        self, method: str, path: str, timeout: float, body: JsonObject | None = None
    ) -> JsonObject:
        headers = {"Accept": "application/json"}
        if self._read_token is not None:
            headers["Authorization"] = f"Bearer {self._read_token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            with requests.Session() as client:
                client.trust_env = False
                response = client.request(
                    method,
                    f"{self._base_url}/api/v1{path}",
                    headers=headers,
                    json=body,
                    timeout=timeout,
                    allow_redirects=False,
                )
        except requests.RequestException:
            raise KnowledgeError(
                "knowledge service request failed; check the URL and network"
            ) from None
        if not 200 <= response.status_code < 300:
            raise KnowledgeError(f"knowledge service returned HTTP {response.status_code}")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise KnowledgeError("knowledge service response exceeded 1 MiB")
        try:
            result = response.json()
            json.dumps(result, allow_nan=False)
        except (requests.JSONDecodeError, ValueError, TypeError):
            raise KnowledgeError("knowledge service returned invalid JSON") from None
        if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
            raise KnowledgeError("knowledge service response must be a JSON object")
        return result


def _validated_id(value: JsonValue, name: str) -> str:
    if not isinstance(value, str) or _DOCUMENT_ID.fullmatch(value) is None:
        raise KnowledgeError(f"invalid {name}")
    return value


def _fits_result(result: JsonObject) -> bool:
    return len(json.dumps({"result": result, "truncated": False}, ensure_ascii=True)) <= (
        _MAX_RESULT_BYTES
    )


def _bounded_text(result: JsonObject, text_holder: JsonObject) -> None:
    text = text_holder.get("text")
    if not isinstance(text, str):
        raise KnowledgeError("knowledge response is too large and has no reducible text")
    text_holder["client_text_truncated"] = True
    text_holder["client_original_text_characters"] = len(text)
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        text_holder["text"] = text[:middle]
        if _fits_result(result):
            low = middle
        else:
            high = middle - 1
    text_holder["text"] = text[:low]
    if not _fits_result(result) or (not low and text):
        raise KnowledgeError("knowledge response metadata exceeds the result size limit")


def _bounded_result(result: JsonObject, kind: str) -> JsonObject:
    """Keep knowledge results structured and mark any client-side size truncation."""
    if _fits_result(result):
        return result
    if kind == "section":
        _bounded_text(result, result)
        return result

    container = result if kind == "search" else result.get("table_of_contents")
    if not isinstance(container, dict):
        raise KnowledgeError("knowledge response cannot be bounded")
    key = "results" if kind == "search" else "sections"
    items = container.get(key)
    if not isinstance(items, list) or not items:
        raise KnowledgeError("knowledge response metadata exceeds the result size limit")
    container["client_truncated"] = True
    if kind == "document":
        container["has_more"] = True
    low, high = 0, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        container[key] = items[:middle]
        container["client_omitted_items"] = len(items) - middle
        if _fits_result(result):
            low = middle
        else:
            high = middle - 1
    container[key] = items[:low]
    container["client_omitted_items"] = len(items) - low
    if low:
        return result
    if kind == "search" and isinstance(items[0], dict):
        container[key] = [items[0]]
        container["client_omitted_items"] = len(items) - 1
        _bounded_text(result, items[0])
        return result
    raise KnowledgeError("knowledge response metadata exceeds the result size limit")
