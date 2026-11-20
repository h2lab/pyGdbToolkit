# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Build and validate the dossier exported by the dashboard."""

from datetime import UTC, datetime
from importlib.resources import files
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .llm.confidentiality import Redactor
from .llm.contracts import JsonObject, JsonValue

_SCHEMA = "schemas/audit-report-v1.schema.json"
_SECRET_KEY = re.compile(
    r"password|passwd|pwd|secret|api[_-]?key|(?:access[_-]?|auth[_-]?)?token|authorization",
    re.IGNORECASE,
)
_REFERENCE_KEYS = {
    "artifact_sha256",
    "sha256",
    "digest",
    "call_id",
    "context_id",
    "session_id",
    "plan_id",
    "target_context_id",
}


def _mask(value: JsonValue, redactor: Redactor) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED:credential-field]"
                if _SECRET_KEY.fullmatch(key)
                else item if key in _REFERENCE_KEYS else _mask(item, redactor)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item, redactor) for item in value]
    return redactor.text(value)[0] if isinstance(value, str) else value


def _mask_event(kind: JsonValue, payload: JsonObject, redactor: Redactor) -> JsonObject:
    result: JsonObject = dict(payload)
    for key, value in result.items():
        if key not in {
            "session_id",
            "context_id",
            "session_created_at",
            "stage",
            "operation_id",
            "request_id",
            "snapshot_sequence",
            "outcome",
        }:
            result[key] = _mask(value, redactor)
    if kind == "llm_tool_result":
        data, masked_data = payload.get("data"), result.get("data")
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, str) and isinstance(masked_data, dict):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                pass
            else:
                masked = _mask(parsed, redactor)
                masked_data["content"] = (
                    json.dumps(masked, ensure_ascii=False) if masked != parsed else content
                )
    if kind == "snapshot" and isinstance(result["data"], dict):
        source = payload["data"]
        if isinstance(source, dict):
            result["data"]["captured_at"] = source["captured_at"]
            project = result["data"]["project"]
            original = source["project"]
            if isinstance(project, dict) and isinstance(original, dict) and "loaded_at" in original:
                project["loaded_at"] = original["loaded_at"]
    return result


def make_report(
    metadata: JsonObject, entries: list[JsonObject], redactor: Redactor | None
) -> JsonObject:
    """Project the ordered SQLite records into the versioned report contract."""
    audit = dict(metadata)
    audit["status"] = "open" if audit["stopped_at"] is None else "closed"
    if redactor is not None:
        audit["title"] = _mask(audit["title"], redactor)
    events: list[JsonValue] = []
    gaps: list[JsonValue] = []
    snapshots: list[JsonValue] = []
    sessions: dict[str, JsonValue] = {}
    pending: dict[str, int] = {}
    sequenced_gaps: list[tuple[int, JsonObject]] = []
    for entry in entries:
        kind = entry["kind"]
        payload = entry["payload"]
        if not isinstance(payload, dict):
            raise ValueError("invalid audit event payload")
        if redactor is not None:
            payload = _mask_event(kind, payload, redactor)
        sequence = entry["sequence"]
        if not isinstance(sequence, int):
            raise ValueError("invalid audit sequence")
        if kind == "snapshot":
            snapshots.append({"sequence": sequence, **payload})
            data = payload["data"]
            if isinstance(data, dict):
                for part, observation in data.items():
                    if isinstance(observation, dict) and "unavailable_reason" in observation:
                        sequenced_gaps.append(
                            (
                                sequence,
                                {
                                    "sequence": sequence,
                                    "section": part,
                                    "reason": observation["unavailable_reason"],
                                },
                            )
                        )
            payload = {"stage": payload["stage"], "snapshot_sequence": sequence}
        elif kind == "llm_context":
            session = payload["session_id"]
            if not isinstance(session, str):
                raise ValueError("invalid audit session ID")
            sessions[session] = {
                "id": session,
                "provider": payload["provider"],
                "created_at": payload["session_created_at"],
            }
        elif kind == "gap":
            sequenced_gaps.append((sequence, {"sequence": sequence, **payload}))
        elif kind == "manual_start" and isinstance(operation := payload["operation_id"], str):
            pending[operation] = sequence
        elif kind == "manual_result" and isinstance(operation := payload["operation_id"], str):
            pending.pop(operation, None)
            if payload["outcome"] == "cancelled":
                sequenced_gaps.append(
                    (sequence, {"sequence": sequence, "reason": "manual command cancelled"})
                )
        elif kind == "llm_tool_result":
            result = payload.get("data")
            content = result.get("content") if isinstance(result, dict) else None
            if isinstance(content, str):
                try:
                    decoded = json.loads(content)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, dict) and decoded.get("truncated") is True:
                    sequenced_gaps.append(
                        (sequence, {"sequence": sequence, "reason": "tool result truncated"})
                    )
        events.append({**entry, "payload": payload})
    for sequence in pending.values():
        sequenced_gaps.append(
            (sequence, {"sequence": sequence, "reason": "no manual command result recorded"})
        )
    gaps.extend(gap for _, gap in sorted(sequenced_gaps, key=lambda pair: pair[0]))
    return {
        "$schema": "https://h2lab.github.io/pyGdbToolkit/schemas/audit-report-v1.schema.json",
        "schema_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "redacted": redactor is not None,
        "audit": audit,
        "snapshots": snapshots,
        "sessions_llm": list(sessions.values()),
        "events": events,
        "collection_gaps": gaps,
    }


def write_report(path: Path, document: JsonObject) -> None:
    """Validate and atomically replace the destination with a private JSON file."""
    schema = json.loads(files("pyGdbClient").joinpath(_SCHEMA).read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except ValidationError as error:
        raise ValueError(f"audit report does not match the schema at {error.json_path}") from None
    text = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, mode="w", encoding="utf-8", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
