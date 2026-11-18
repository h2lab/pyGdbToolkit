# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Load and validate the initial declarative scenario format before execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml
from websockets.exceptions import InvalidURI

from .farm import Target


@dataclass(frozen=True)
class ConnectAll:
    """Connect all declared servers."""

    action: ClassVar[str] = "connect_all"


@dataclass(frozen=True)
class ResetSequence:
    """Reset and halt selected targets in parallel, then wait for a delay."""

    targets: tuple[str, ...]
    delay_ms: int = 0
    action: ClassVar[str] = "reset_sequence"


@dataclass(frozen=True)
class RunUntil:
    """Resume a target and wait for an actual GDB stop at an address."""

    target: str
    address: int
    timeout_ms: int | None = None
    action: ClassVar[str] = "run_until"


@dataclass(frozen=True)
class PeerFailure:
    """Inject a reset into a single peer."""

    target: str
    command: str
    action: ClassVar[str] = "peer_failure"


@dataclass(frozen=True)
class WaitAndObserve:
    """Sample target states and collect logs for the observation interval."""

    duration_ms: int
    monitor: tuple[str, ...]
    poll_interval_ms: int = 100
    action: ClassVar[str] = "wait_and_observe"


@dataclass(frozen=True)
class CommandStep:
    """Execute a command on selected targets with a descriptive action label."""

    action: str
    targets: tuple[str, ...]
    command: str


type Step = ConnectAll | ResetSequence | RunUntil | PeerFailure | WaitAndObserve | CommandStep


@dataclass(frozen=True)
class Scenario:
    """A validated scenario with identified targets and ordered steps."""

    name: str
    targets: tuple[Target, ...]
    steps: tuple[Step, ...]


class _ScenarioLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys instead of silently replacing operations."""


def _yaml_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError("scenario mapping keys must be strings")
        if key in mapping:
            raise ValueError(f"duplicate scenario key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_ScenarioLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be a mapping with string keys")
    return value


def _fields(data: dict[str, Any], required: set[str], optional: set[str], context: str) -> None:
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise ValueError(f"{context}: missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{context}: unknown fields: {', '.join(sorted(unknown))}")


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _integer(value: object, context: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{context} must be an integer >= {minimum}")
    return value


def _list(value: object, context: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context} must be a non-empty list")
    return value


def _target_id(value: object, identifiers: set[str], context: str) -> str:
    identifier = _text(value, context)
    if identifier not in identifiers:
        raise ValueError(f"{context}: unknown target id: {identifier}")
    return identifier


def _selection(value: object, identifiers: set[str], context: str) -> tuple[str, ...]:
    selected = tuple(_target_id(item, identifiers, context) for item in _list(value, context))
    if len(set(selected)) != len(selected):
        raise ValueError(f"{context}: duplicate target ids")
    return selected


def _step(value: object, identifiers: set[str], index: int) -> Step:
    context = f"steps[{index}]"
    data = _mapping(value, context)
    action = _text(data.get("action"), f"{context}.action")
    match action:
        case "connect_all":
            _fields(data, {"action"}, set(), context)
            if index != 0:
                raise ValueError("connect_all must appear exactly once, as the first step")
            return ConnectAll()
        case "reset_sequence":
            _fields(data, {"action", "targets"}, {"delay_ms"}, context)
            return ResetSequence(
                _selection(data["targets"], identifiers, f"{context}.targets"),
                _integer(data.get("delay_ms", 0), f"{context}.delay_ms"),
            )
        case "run_until":
            _fields(data, {"action", "target", "address"}, {"timeout_ms"}, context)
            address = data["address"]
            if isinstance(address, str):
                try:
                    address = int(address, 0)
                except ValueError as error:
                    raise ValueError(
                        f"{context}.address must be an integer or hex address"
                    ) from error
            address = _integer(address, f"{context}.address")
            if address > 0xFFFFFFFFFFFFFFFF:
                raise ValueError(f"{context}.address must fit in 64 bits")
            timeout_ms = (
                _integer(data["timeout_ms"], f"{context}.timeout_ms", 1)
                if "timeout_ms" in data
                else None
            )
            return RunUntil(
                _target_id(data["target"], identifiers, f"{context}.target"), address, timeout_ms
            )
        case "peer failure" | "peer_failure":
            _fields(data, {"action", "target", "command"}, set(), context)
            command = _text(data["command"], f"{context}.command")
            if command != "reset":
                raise ValueError(f"{context}.command: only 'reset' is currently supported")
            return PeerFailure(
                _target_id(data["target"], identifiers, f"{context}.target"), command
            )
        case "wait_and_observe":
            _fields(data, {"action", "duration_ms", "monitor"}, {"poll_interval_ms"}, context)
            return WaitAndObserve(
                _integer(data["duration_ms"], f"{context}.duration_ms"),
                _selection(data["monitor"], identifiers, f"{context}.monitor"),
                _integer(data.get("poll_interval_ms", 100), f"{context}.poll_interval_ms", 1),
            )
        case _:
            if "command" not in data:
                raise ValueError(f"{context}: unsupported action: {action}")
            _fields(data, {"action", "targets", "command"}, set(), context)
            return CommandStep(
                action,
                _selection(data["targets"], identifiers, f"{context}.targets"),
                _text(data["command"], f"{context}.command"),
            )


def parse_scenario(document: object) -> Scenario:
    """Validate all fields and target references without touching any server."""
    data = _mapping(document, "scenario document")
    _fields(data, {"scenario", "targets", "steps"}, set(), "scenario document")
    name = _text(data["scenario"], "scenario")
    targets: list[Target] = []
    identifiers: set[str] = set()
    for index, value in enumerate(_list(data["targets"], "targets")):
        context = f"targets[{index}]"
        target = _mapping(value, context)
        _fields(target, {"id", "server"}, {"role"}, context)
        identifier = _text(target["id"], f"{context}.id")
        if identifier in identifiers:
            raise ValueError(f"duplicate target id: {identifier}")
        role = _text(target["role"], f"{context}.role") if "role" in target else None
        try:
            targets.append(Target(identifier, _text(target["server"], f"{context}.server"), role))
        except InvalidURI as error:
            raise ValueError(f"{context}.server: {error}") from error
        identifiers.add(identifier)
    steps = tuple(
        _step(value, identifiers, index)
        for index, value in enumerate(_list(data["steps"], "steps"))
    )
    if not isinstance(steps[0], ConnectAll):
        raise ValueError("the first step must be connect_all")
    return Scenario(name, tuple(targets), steps)


def load_scenario(path: Path) -> Scenario:
    """Load a safe YAML (or JSON) scenario and validate it completely."""
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_ScenarioLoader)
    except (yaml.YAMLError, UnicodeError) as error:
        raise ValueError(f"invalid scenario file {path}: {error}") from error
    return parse_scenario(document)
