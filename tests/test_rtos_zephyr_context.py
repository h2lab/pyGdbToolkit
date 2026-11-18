# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded Cortex-M Zephyr saved/live context and stack measurements."""

from io import StringIO
from pathlib import Path
import struct
from types import ModuleType
from unittest.mock import Mock

import pytest
from rich.console import Console

from pyGdbToolkit.rtos import zephyr_context as context
from pyGdbToolkit.target_memory import TargetReadError

START = 0x20001000
PSP = START + 128
CONFIG = {
    "CONFIG_CPU_CORTEX_M": "y",
    "CONFIG_THREAD_STACK_INFO": "y",
    "CONFIG_INIT_STACKS": "n",
    "CONFIG_FPU": "n",
}
CALLEE = {f"r{i}": 0x4000 + i for i in range(4, 12)}
WORDS = (0, 1, 2, 3, 12, 0x08002001, 0x08001000, 0x01000000)


class Memory:
    """Exact reader that records every range and rejects overreads."""

    def __init__(self, data: bytes, start: int = START) -> None:
        self.data = data
        self.start = start
        self.reads: list[tuple[int, int]] = []

    def read_bytes(self, address: int, size: int) -> bytes:
        """Return only a fully present range."""
        self.reads.append((address, size))
        offset = address - self.start
        if offset < 0 or offset + size > len(self.data):
            raise TargetReadError(address, size, "outside fixture")
        return self.data[offset : offset + size]

    def read_uint16(self, address: int) -> int:
        """Read a little-endian halfword."""
        return int.from_bytes(self.read_bytes(address, 2), "little")

    def read_uint32(self, address: int) -> int:
        """Read a little-endian word."""
        return int.from_bytes(self.read_bytes(address, 4), "little")


def _value(fields: dict[str, object]) -> Mock:
    result = Mock()
    result.__getitem__ = Mock(side_effect=fields.__getitem__)
    return result


def _thread(*, delta: int = 16, exc_return: int = 0xFD) -> Mock:
    return _value(
        {
            "stack_info": _value({"start": START, "size": 512, "delta": delta}),
            "callee_saved": _value(
                {"psp": PSP, **{f"v{i}": CALLEE[f"r{i + 3}"] for i in range(1, 9)}}
            ),
            "arch": _value({"mode_exc_return": exc_return}),
        }
    )


def _render(
    thread: Mock, current: bool = False, config: dict[str, str] | None = None, cpu_count: int = 1
) -> str:
    stream = StringIO()
    Console(file=stream, width=160).print(
        context.show_context(thread, current, cpu_count, CONFIG if config is None else config)
    )
    return stream.getvalue()


@pytest.fixture
def memory(fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Memory:
    """Expose the exact synthetic stack through the standard target-memory reader."""
    data = b"\xaa" * 128 + struct.pack("<8I", *WORDS) + b"\x55" * (512 - 160)
    memory = Memory(data)
    inferior = Mock()
    inferior.read_memory.side_effect = memory.read_bytes
    monkeypatch.setattr(fake_gdb, "selected_inferior", Mock(return_value=inferior))
    monkeypatch.setattr(
        fake_gdb, "execute", Mock(return_value="function + 4 in section .text"), raising=False
    )
    block = Mock()
    block.function.print_name = "function"
    block.function.symtab.objfile.filename = "/build/zephyr/zephyr.elf"
    block.start = WORDS[6] - 4
    monkeypatch.setattr(fake_gdb, "block_for_pc", Mock(return_value=block), raising=False)
    return memory


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("aligned_padding", [False, True])
def test_saved_frame_layout_and_restored_sp(extended: bool, aligned_padding: bool) -> None:
    """The core frame starts at saved PSP; FP storage follows it in Zephyr's arch_esf."""
    words = (*WORDS[:-1], WORDS[-1] | (0x200 if aligned_padding else 0))
    data = struct.pack("<8I", *words) + bytes(76)
    memory = Memory(data, PSP)
    saved = context.decode_saved_context(
        memory, context.StackBounds(START, 512, 16), PSP, CALLEE, 0xED if extended else 0xFD
    )
    expected_size = (104 if extended else 32) + (4 if aligned_padding else 0)
    assert saved.restored_sp == PSP + expected_size
    assert saved.registers["sp"] == PSP + expected_size
    assert saved.registers["r0"] == 0
    assert saved.registers["lr"] == 0x08002001
    assert saved.registers["pc"] == 0x08001000
    assert all(saved.registers[key] == value for key, value in CALLEE.items())
    assert saved.extended is extended
    assert saved.frame_bytes == expected_size
    assert memory.reads == [(PSP, 32)]


@pytest.mark.parametrize(
    "start,size,delta",
    [
        (0, 512, 0),
        (START + 1, 512, 0),
        (START, 0, 0),
        (START, 512, -1),
        (START, 512, 512),
        (START, 512, 1),
        (0xFFFFFF00, 512, 0),
    ],
)
def test_corrupt_stack_bounds_fail(start: int, size: int, delta: int) -> None:
    """Never use wrapping, inverted, empty or misaligned stack ranges."""
    with pytest.raises(ValueError, match="stack_info bounds"):
        context.StackBounds(start, size, delta)


@pytest.mark.parametrize("psp", [START - 4, START + 1, START + 480, START + 512])
def test_saved_frame_must_fit_complete_usable_bounds(psp: int) -> None:
    """Top delta is not available for exception-frame reads."""
    memory = Memory(b"\0" * 512)
    with pytest.raises(ValueError, match="outside bounds"):
        context.decode_saved_context(memory, context.StackBounds(START, 512, 16), psp, CALLEE, 0xFD)
    assert not memory.reads


def test_xpsr_padding_must_also_fit_inside_stack() -> None:
    """The optional alignment word is included in validation, not just the core words."""
    psp = START + 480
    memory = Memory(struct.pack("<8I", *WORDS[:-1], WORDS[-1] | 0x200), psp)
    with pytest.raises(ValueError, match="outside bounds"):
        context.decode_saved_context(memory, context.StackBounds(START, 512, 0), psp, CALLEE, 0xFD)


@pytest.mark.parametrize("xpsr,pc", [(0, 0x08001000), (0x0100000E, 0x08001000), (0x01000000, 0)])
def test_saved_frames_require_valid_thumb_thread_state(xpsr: int, pc: int) -> None:
    """Corrupt or Handler-mode words cannot be reported as a resumable thread context."""
    memory = Memory(struct.pack("<8I", *WORDS[:6], pc, xpsr), PSP)
    with pytest.raises(ValueError, match="Invalid Zephyr saved"):
        context.decode_saved_context(memory, context.StackBounds(START, 512, 0), PSP, CALLEE, 0xFD)


@pytest.mark.parametrize("exc_return", [0, 0xF9, 0xF1, 0xBC, 0x1FD])
def test_other_stack_modes_or_security_returns_are_rejected(exc_return: int) -> None:
    """No guessed frame layout for MSP, secure transitions or corrupt EXC_RETURN."""
    memory = Memory(b"")
    with pytest.raises(ValueError, match="EXC_RETURN octet"):
        context.decode_saved_context(
            memory, context.StackBounds(START, 512, 0), PSP, CALLEE, exc_return
        )
    assert not memory.reads


def test_stack_fill_scan_excludes_delta_and_sentinel() -> None:
    """TLS/randomization and the sentinel are not mistaken for touched thread stack."""
    memory = Memory(b"\xf0" * 4 + b"\xaa" * 300 + b"\0" * 192 + b"\xbb" * 16)
    unused, capacity = context.stack_high_water(memory, context.StackBounds(START, 512, 16), True)
    assert (unused, capacity) == (300, 492)
    assert memory.reads == [(START + 4, 256), (START + 260, 236)]
    assert max(address + size for address, size in memory.reads) == START + 496


@pytest.mark.parametrize("size", [context.MAX_STACK_SCAN, context.MAX_STACK_SCAN + 4])
def test_scan_budget_boundary_is_real_not_a_reduced_proxy(size: int) -> None:
    """Accept one MiB exactly and reject a larger range before reading it."""
    memory = Memory(b"\xaa" * size)
    if size == context.MAX_STACK_SCAN:
        assert context.stack_high_water(memory, context.StackBounds(START, size, 0), False) == (
            size,
            size,
        )
        assert len(memory.reads) == size // 256
    else:
        with pytest.raises(ValueError, match="fill scan requires"):
            context.stack_high_water(memory, context.StackBounds(START, size, 0), False)
        assert not memory.reads


def test_saved_context_output_is_evidence_not_a_fabricated_full_unwind(memory: Memory) -> None:
    """Saved core registers and symbolized PC/LR are distinct from a full GDB backtrace."""
    output = _render(_thread())
    assert "Saved callee_saved.psp" in output
    assert "0x20001080" in output
    assert "0x200010A0" in output
    assert "368/496 bytes" in output
    assert "Saved PC" in output and "function + 4" in output
    assert "not a full unwind" in output
    assert "CONFIG_INIT_STACKS is disabled" in output
    assert memory.reads == [(PSP, 32)]


def test_fill_estimate_is_opt_in_and_labeled(memory: Memory) -> None:
    """No high-water claim without INIT_STACKS, and enabled scans identify their approximation."""
    output = _render(_thread(), config={**CONFIG, "CONFIG_INIT_STACKS": "y"})
    assert "368/496 bytes touched" in output
    assert "pattern estimate, not proof" in output
    assert len(memory.reads) > 1


def test_saved_symbol_evidence_rejects_another_loaded_image(
    memory: Memory, fake_gdb: ModuleType
) -> None:
    """Keep raw saved addresses rather than attach another project's function name."""
    fake_gdb.block_for_pc.return_value.function.symtab.objfile.filename = "/old/image.elf"
    stream = StringIO()
    Console(file=stream, width=160).print(
        context.show_context(_thread(), False, 1, CONFIG, Path("/build/zephyr/zephyr.elf"))
    )
    output = stream.getvalue()
    assert "0x08001000" in output
    assert "symbol unavailable in loaded project" in output
    assert "function +" not in output
    fake_gdb.execute.assert_not_called()


@pytest.mark.parametrize(
    "option",
    [
        "CONFIG_USE_SWITCH",
        "CONFIG_USERSPACE",
        "CONFIG_TRUSTED_EXECUTION_SECURE",
        "CONFIG_TRUSTED_EXECUTION_NONSECURE",
        "CONFIG_ARM_SECURE_FIRMWARE",
        "CONFIG_STACK_GROWS_UP",
    ],
)
def test_unsupported_layouts_do_not_read_any_stack(option: str, memory: Memory) -> None:
    """Thread identity remains usable while an unsupported stack layout is explicit."""
    output = _render(_thread(), config={**CONFIG, option: "y"})
    assert "Stack/context unavailable" in output and option in output
    assert not memory.reads


def test_missing_stack_info_and_unsaved_fpu_state_are_explicit(memory: Memory) -> None:
    """Unavailable bounds or frame-type metadata do not authorize a guessed memory read."""
    assert "CONFIG_THREAD_STACK_INFO" in _render(_thread(), config={"CONFIG_CPU_CORTEX_M": "y"})
    output = _render(_thread(), config={**CONFIG, "CONFIG_FPU": "y"})
    assert "FPU build lacks saved EXC_RETURN" in output
    assert not memory.reads


def test_unrecorded_fpu_is_not_assumed_disabled(memory: Memory) -> None:
    """Without EXC_RETURN, an unknown FP configuration cannot imply a basic frame."""
    config = {key: value for key, value in CONFIG.items() if key != "CONFIG_FPU"}
    assert "CONFIG_FPU is not recorded" in _render(_thread(), config=config)
    assert not memory.reads


def test_extended_frame_requires_consistent_fpu_configuration(memory: Memory) -> None:
    """EXC_RETURN's FP flag and the effective image configuration must agree."""
    with pytest.raises(ValueError, match="contradicts"):
        _render(_thread(exc_return=0xED), config={**CONFIG, "CONFIG_ARM_STORE_EXC_RETURN": "y"})
    output = _render(
        _thread(exc_return=0xED),
        config={
            **CONFIG,
            "CONFIG_FPU": "y",
            "CONFIG_FPU_SHARING": "y",
            "CONFIG_ARM_STORE_EXC_RETURN": "y",
        },
    )
    assert "104 bytes; extended=True" in output
    assert "0x200010E8" in output


@pytest.fixture
def live_frame(fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Expose newest-frame registers independently of any user-selected older frame."""
    values = {f"r{i}": i for i in range(13)}
    values.update(sp=PSP, lr=0x08002001, pc=0x08001000, xpsr=0x01000000)
    frame = Mock()
    frame.read_register.side_effect = values.__getitem__
    frame.pc.return_value = values["pc"]
    frame.name.return_value = "[red]live[/red]"
    frame.older.return_value = None
    monkeypatch.setattr(fake_gdb, "newest_frame", Mock(return_value=frame), raising=False)
    monkeypatch.setattr(
        fake_gdb, "selected_frame", Mock(side_effect=AssertionError("not selected"))
    )
    return frame


def test_live_context_uses_newest_frame_without_mutation(
    memory: Memory, live_frame: Mock, fake_gdb: ModuleType
) -> None:
    """Never substitute stale callee_saved state or issue GDB writes/frame switches."""
    output = _render(_thread(), current=True)
    assert "Live newest GDB frame" in output
    assert "GDB #0" in output and "[red]live[/red]" in output
    assert "368/496 bytes" in output
    assert "Saved PC" not in output
    assert not memory.reads
    fake_gdb.execute.assert_not_called()
    fake_gdb.selected_frame.assert_not_called()


def test_handler_mode_does_not_use_stale_current_thread_frame(
    memory: Memory, live_frame: Mock
) -> None:
    """The scheduled thread's saved PSP is stale while current, including in an ISR."""
    live_frame.read_register.side_effect = lambda name: 0x0100000E if name == "xpsr" else 0
    output = _render(_thread(), current=True)
    assert "Unavailable in Handler mode" in output
    assert "Core registers" not in output
    assert not memory.reads


def test_invalid_live_thumb_state_is_an_error(memory: Memory, live_frame: Mock) -> None:
    """A Cortex-M Thread-mode sample without T cannot be reported as valid registers."""
    live_frame.read_register.side_effect = lambda name: 0
    with pytest.raises(ValueError, match="expected Thumb state"):
        _render(_thread(), current=True)
    assert not memory.reads


def test_missing_newest_frame_is_an_error(
    memory: Memory, live_frame: Mock, fake_gdb: ModuleType
) -> None:
    """Never use saved current-thread state as a fallback for inaccessible live registers."""
    fake_gdb.newest_frame.return_value = None
    with pytest.raises(ValueError, match="no newest GDB frame"):
        _render(_thread(), current=True)
    assert not memory.reads


def test_live_multicore_mapping_is_not_guessed(memory: Memory, live_frame: Mock) -> None:
    """CPU role alone does not bind a GDB register set to that CPU."""
    output = _render(_thread(), current=True, cpu_count=2)
    assert "cannot bind GDB registers" in output
    live_frame.read_register.assert_not_called()
    assert not memory.reads


def test_backtrace_is_bounded_and_allows_equal_sp_inline_frames(
    memory: Memory, live_frame: Mock
) -> None:
    """Inlining/tail calls can share SP; bound the walk without false cycle claims."""
    live_frame.older.return_value = live_frame
    output = _render(_thread(), current=True)
    assert "GDB #63" in output
    assert "Stopped at 64 GDB frames" in output
    assert live_frame.older.call_count == 64


def test_target_read_errors_do_not_become_a_successful_empty_context(
    memory: Memory, fake_gdb: ModuleType
) -> None:
    """Report the failing range through the command boundary."""
    fake_gdb.selected_inferior.return_value.read_memory.side_effect = fake_gdb.MemoryError("denied")
    with pytest.raises(ValueError, match="Cannot read Zephyr stack/context.*denied"):
        _render(_thread())
