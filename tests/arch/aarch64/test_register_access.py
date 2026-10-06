# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for GDB CPU registers and the explicitly limited J-Link CP15 fallback."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyGdbToolkit.arch.aarch64 import session_state
from pyGdbToolkit.ocd import OcdIdentifier, OcdInfo


def test_named_gdb_register_has_priority(fake_gdb: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Standard register access never invokes backend-specific monitor commands."""
    fake_gdb._frame = SimpleNamespace(read_register=lambda name: 0x123456789ABCDEF0)
    monkeypatch.setattr(session_state, "get_ocd", lambda: pytest.fail("unexpected OCD probe"))
    register = session_state.GdbCpuRegisterReader().read_register("REVIDR_EL1", 64)
    assert register.value == 0x123456789ABCDEF0
    assert register.valid_bits == 64
    assert register.source == "GDB register"


@pytest.mark.parametrize(
    "name,encoding,value,valid_bits",
    (
        ("MIDR_EL1", "0,0,0,0", 0x410FD034, 64),
        ("REVIDR_EL1", "0,0,0,6", 0x380, 32),
        ("CTR_EL0", "0,0,3,1", 0x84448004, 32),
    ),
)
def test_jlink_reads_only_verified_encodings(
    fake_gdb: object,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    encoding: str,
    value: int,
    valid_bits: int,
) -> None:
    """The CP15 fallback uses AArch64 encodings and retains truncated-width evidence."""
    monkeypatch.setattr(session_state, "get_ocd", lambda: OcdInfo(OcdIdentifier.JLINK))

    def execute(command: str, to_string: bool) -> str:
        assert command == f"monitor cp15 {encoding}"
        assert to_string
        return f"Reading CP15 register ({encoding} = 0x{value:08X})\n"

    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    register = session_state.GdbCpuRegisterReader().read_register(name, 64)
    assert register.value == value
    assert register.valid_bits == valid_bits
    assert register.source == "J-Link CP15"


@pytest.mark.parametrize(
    "backend", (OcdIdentifier.OPENOCD, OcdIdentifier.PYOCD, OcdIdentifier.UNKNOWN)
)
def test_other_servers_do_not_receive_cp15(
    monkeypatch: pytest.MonkeyPatch, backend: OcdIdentifier
) -> None:
    """Backend-specific monitor syntax is never sent to another server."""
    monkeypatch.setattr(session_state, "get_ocd", lambda: OcdInfo(backend))
    register = session_state.GdbCpuRegisterReader().read_register("MIDR_EL1", 64)
    assert register.value is None


@pytest.mark.parametrize(
    "output",
    (
        "Unsupported register !",
        "Reading CP15 register (0,0,0,1 = 0x410FD034)",
        "Reading CP15 register (0,0,0,0 = 0x123456789ABCDEF0)",
    ),
)
def test_invalid_cp15_responses_remain_unavailable(
    fake_gdb: object, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    """An error, wrong register, or unexpected response width is not CPU evidence."""
    monkeypatch.setattr(session_state, "get_ocd", lambda: OcdInfo(OcdIdentifier.JLINK))
    monkeypatch.setattr(fake_gdb, "execute", lambda *args, **kwargs: output, raising=False)
    assert session_state.GdbCpuRegisterReader().read_register("MIDR_EL1", 64).value is None


def test_wide_registers_have_no_truncated_cp15_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wide affinity and AA64 feature registers are not fabricated from 32-bit aliases."""
    monkeypatch.setattr(session_state, "get_ocd", lambda: pytest.fail("unexpected OCD probe"))
    reader = session_state.GdbCpuRegisterReader()
    assert reader.read_register("MPIDR_EL1", 64).value is None
    assert reader.read_register("ID_AA64PFR0_EL1", 64).value is None


@pytest.mark.parametrize("pstate,expected", ((0, 0), (4, 4), (8, 8), (12, 12), (0x10, None)))
def test_current_el_from_aarch64_pstate(
    fake_gdb: object, pstate: int, expected: int | None
) -> None:
    """The exception level is derived from PSTATE only when nRW confirms AArch64."""

    def read_register(name: str) -> int:
        if name == "cpsr":
            return pstate
        raise fake_gdb.error("register unavailable")

    fake_gdb._frame = SimpleNamespace(read_register=read_register)
    assert session_state.GdbCpuRegisterReader().read_register("CurrentEL", 64).value == expected
