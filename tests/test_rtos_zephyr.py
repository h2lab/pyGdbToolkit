# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Zephyr build discovery, linked ELF validation and nonintrusive GDB loading."""

import base64
from io import StringIO
import json
from pathlib import Path
import struct
import subprocess
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest
from rich.console import Console

from pyGdbServer.rtos import RtosProjects
from pyGdbToolkit.cmd_rtos import RtosCmd, load_rtos_project, rtos_status
from pyGdbToolkit.rtos import zephyr
from pyGdbToolkit.shared.rtos import DEFAULT_PROJECT_REGISTRY, project_manifest
from pyGdbToolkit.shared.rtos import zephyr as shared

BUILD_INFO = b"""version: '0.1.0'
cmake:
  application:
    source-dir: /client/workspace/zephyr/samples/basic/blinky
  board:
    name: nucleo_u5a5zj_q
    qualifiers: stm32u5a5xx
  zephyr:
    version: '4.5.99'
    zephyr-base: /client/workspace/zephyr
"""
CONFIG = b"""CONFIG_BOARD="nucleo_u5a5zj_q"
CONFIG_ARCH="arm"
CONFIG_CPU_CORTEX_M=y
CONFIG_THREAD_STACK_INFO=y
CONFIG_MBEDTLS_VERSION_4_x=y
# CONFIG_MBEDTLS_VERSION_3_x is not set
# CONFIG_THREAD_MONITOR is not set
# CONFIG_THREAD_NAME is not set
# CONFIG_DEBUG_THREAD_INFO is not set
# CONFIG_INIT_STACKS is not set
"""


def _elf(*, machine: int = 40, elf_type: int = 2, dwarf: bool = True, kernel: bool = True) -> bytes:
    """Build a tiny linked ELF32 with one real DWARF CU and a kernel symbol."""
    names = (
        "",
        ".text",
        ".bss",
        ".symtab",
        ".strtab",
        ".debug_info",
        ".debug_abbrev",
        ".shstrtab",
    )
    strings = b"\0_kernel\0"
    section_names = "\0".join(names).encode() + b"\0"
    # DWARF v4 CU: 7-byte header, one compile_unit DIE with no children/attributes.
    debug_info = struct.pack("<IHIBB", 8, 4, 0, 4, 1) if dwarf else b""
    contents = (
        b"",
        b"\x00\xbf\x00\xbf",
        b"",
        b"\0" * 16 + struct.pack("<IIIBBH", 1, 0x20000000, 32, 0x11, 0, 2),
        strings if kernel else b"\0missing\0",
        debug_info,
        b"\x01\x11\x00\x00\x00\x00" if dwarf else b"",
        section_names,
    )
    image = bytearray(b"\0" * (52 + 2 * 32))
    sections = [b"\0" * 40]
    offsets = [0]
    for index in range(1, len(names)):
        image.extend(b"\0" * (-len(image) % 4))
        offset = len(image)
        offsets.append(offset)
        image.extend(contents[index])
        section_type = {2: 8, 3: 2, 4: 3, 7: 3}.get(index, 1)
        size = 32 if index == 2 else len(contents[index])
        sections.append(
            struct.pack(
                "<IIIIIIIIII",
                section_names.index(names[index].encode() + b"\0"),
                section_type,
                {1: 6, 2: 3}.get(index, 0),
                {1: 0x08000000, 2: 0x20000000}.get(index, 0),
                offset,
                size,
                4 if index == 3 else 0,
                1 if index == 3 else 0,
                4,
                16 if index == 3 else 0,
            )
        )
    image.extend(b"\0" * (-len(image) % 4))
    section_offset = len(image)
    image.extend(b"".join(sections))
    struct.pack_into(
        "<16sHHIIIIIHHHHHH",
        image,
        0,
        b"\x7fELF\x01\x01\x01" + b"\0" * 9,
        elf_type,
        machine,
        1,
        0x08000001,
        52,
        section_offset,
        0,
        52,
        32,
        2,
        40,
        len(names),
        7,
    )
    struct.pack_into("<IIIIIIII", image, 52, 1, offsets[1], 0x08000000, 0x08000000, 4, 4, 5, 4)
    struct.pack_into("<IIIIIIII", image, 84, 1, offsets[2], 0x20000000, 0x20000000, 0, 32, 6, 4)
    return bytes(image)


@pytest.fixture
def build(tmp_path: Path) -> Path:
    """Create the exact manifest files and extra artifacts that must not be loaded."""
    (tmp_path / "zephyr").mkdir()
    (tmp_path / shared.PROJECT_FILE).write_bytes(BUILD_INFO)
    (tmp_path / shared.KCONFIG).write_bytes(CONFIG)
    (tmp_path / shared.KERNEL_ELF).write_bytes(_elf())
    (tmp_path / shared.DEVICETREE).write_text("/dts-v1/;\n/ {};\n")
    (tmp_path / "zephyr/zephyr_pre0.elf").write_bytes(b"must not be loaded")
    (tmp_path / "compile_commands.json").write_text("[]")
    return tmp_path


def test_shared_manifest_and_identity_are_build_relative() -> None:
    """Absolute compilation paths never become artifact paths or source permissions."""
    bootstrap = project_manifest("zephyr")
    assert bootstrap.project_file == shared.PROJECT_FILE
    assert bootstrap.required == (shared.PROJECT_FILE,)
    complete = project_manifest("zephyr", BUILD_INFO)
    assert complete.required == shared.REQUIRED
    assert complete.required_patterns == ()
    assert all(complete.permits(path) for path in shared.REQUIRED)
    for path in ("../zephyr/zephyr.elf", "zephyr/zephyr_pre0.elf", "edt.pickle", "CMakeCache.txt"):
        assert not complete.permits(path)
    description = DEFAULT_PROJECT_REGISTRY.describe("zephyr", BUILD_INFO)
    assert description.name == "blinky"
    assert description.version is None  # The application version is not in this metadata.
    assert [component.kind for component in description.components] == ["application", "kernel"]
    assert description.components[1].revision == "4.5.99"
    assert all(component.sources is None for component in description.components)
    assert "/client/" not in json.dumps(description.to_dict())


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"[]",
        b"version: [",
        b"\xff",
        BUILD_INFO.replace(b"'0.1.0'", b"'2.0.0'"),
        BUILD_INFO.replace(b"  board:", b"  other:"),
        BUILD_INFO.replace(b"nucleo_u5a5zj_q", b"[]"),
        BUILD_INFO.replace(b"'4.5.99'", b"null"),
        BUILD_INFO.replace(b"/client/workspace/zephyr/samples/basic/blinky", b"/"),
        BUILD_INFO.replace(b"stm32u5a5xx", b"123"),
        BUILD_INFO.replace(b"version: '0.1.0'", b"version: !!python/object:builtins.object {}"),
        b"x" * (shared.MAX_METADATA_SIZE + 1),
    ],
)
def test_shared_metadata_errors_are_explicit(content: bytes) -> None:
    """Malformed or unknown build schemas fail in both discovery and description."""
    with pytest.raises(ValueError, match="build_info.yml"):
        project_manifest("zephyr", content)
    with pytest.raises(ValueError, match="build_info.yml"):
        DEFAULT_PROJECT_REGISTRY.describe("zephyr", content)


def test_shared_zephyr_import_is_host_safe() -> None:
    """Discovery must not import GDB, ELF decoding or toolkit live services."""
    script = """
import sys
from pyGdbToolkit.shared.rtos import project_manifest
assert project_manifest("zephyr").project_file == "build_info.yml"
assert "gdb" not in sys.modules
assert "elftools" not in sys.modules
assert "pyGdbToolkit.rtos" not in sys.modules
"""
    subprocess.run([sys.executable, "-I", "-c", script], check=True)


def test_loads_only_final_image_and_keeps_status_nonintrusive(
    build: Path,
    fake_gdb: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The local and RPC entrypoints load the same context without a target."""
    execute = Mock(return_value="")
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    memory = Mock(side_effect=AssertionError("must not access target"))
    monkeypatch.setattr(fake_gdb, "selected_inferior", memory)
    snapshot = load_rtos_project("zephyr", str(build))
    execute.assert_called_once_with(
        f'add-symbol-file "{build / shared.KERNEL_ELF}" -o 0', from_tty=False, to_string=True
    )
    assert snapshot["selected_rtos"] == "zephyr"
    assert snapshot["loaded"] is True
    assert snapshot["task_list"] is None
    command = RtosCmd()
    project = command.state.project
    assert isinstance(project, zephyr.ZephyrProject)
    assert project.regions == (
        zephyr.LoadRegion(0x08000000, 4, 0x08000000, 4, 5),
        zephyr.LoadRegion(0x20000000, 32, 0x20000000, 0, 6),
    )
    assert project.config["CONFIG_THREAD_MONITOR"] == "n"
    assert project.config["CONFIG_MBEDTLS_VERSION_4_x"] == "y"
    assert project.config["CONFIG_MBEDTLS_VERSION_3_x"] == "n"
    # Cached build display and status remain available after build files are removed.
    for relative in shared.REQUIRED:
        (build / relative).unlink()
    command._invoke_show([])
    assert rtos_status() == snapshot
    output = capsys.readouterr().out
    assert "blinky" in output and "nucleo_u5a5zj_q" in output
    assert "0x08000000-0x08000004" in output
    assert "0x20000000-0x20000020" in output
    assert "R-X" in output and "RW-" in output
    with pytest.raises(fake_gdb.GdbError, match="unavailable for this zephyr build"):
        command._invoke_showsched(["3"])
    memory.assert_not_called()


@pytest.mark.parametrize("relative", shared.REQUIRED)
def test_missing_artifact_fails_before_symbol_loading(
    build: Path, relative: str, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incomplete builds never appear loaded or partially add their symbols."""
    (build / relative).unlink()
    execute = Mock()
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    with pytest.raises(fake_gdb.GdbError, match="Cannot load RTOS project"):
        load_rtos_project("zephyr", str(build))
    execute.assert_not_called()
    assert not rtos_status()["loaded"]


@pytest.mark.parametrize(
    "config,reason",
    [
        (CONFIG.replace(b"nucleo_u5a5zj_q", b"another_board"), "board mismatch"),
        (CONFIG.replace(b"CONFIG_CPU_CORTEX_M=y", b"# CONFIG_CPU_CORTEX_M is not set"), "Cortex-M"),
        (CONFIG.replace(b'"arm"', b'"riscv"'), "Cortex-M"),
        (CONFIG.replace(b'CONFIG_ARCH="arm"', b"CONFIG_ARCH=arm"), "CONFIG_ARCH"),
        (CONFIG + b"bad config\n", "Invalid Zephyr .config"),
        (CONFIG + b'CONFIG_BOARD="duplicate"\n', "duplicate"),
        (CONFIG + b"CONFIG_THREAD_NAME=y\n", "duplicate"),
        (b"\xff", "UTF-8"),
        (b"x" * (shared.MAX_METADATA_SIZE + 1), "size limit"),
    ],
)
def test_invalid_config_fails_before_symbols(
    build: Path, config: bytes, reason: str, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad, incompatible or inconsistent effective configuration cannot be hidden."""
    (build / shared.KCONFIG).write_bytes(config)
    execute = Mock()
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    with pytest.raises(fake_gdb.GdbError, match=reason):
        load_rtos_project("zephyr", str(build))
    execute.assert_not_called()
    assert not rtos_status()["loaded"]


@pytest.mark.parametrize(
    "image,reason",
    [
        (b"not an ELF", "ELF"),
        (_elf(machine=62), "32-bit little-endian Arm"),
        (_elf(elf_type=1), "linked"),
        (_elf(dwarf=False), "DWARF"),
        (_elf(kernel=False), "_kernel"),
        (_elf()[:100], "ELF"),
    ],
)
def test_invalid_elf_fails_before_symbols(
    build: Path, image: bytes, reason: str, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-link, stripped, foreign and truncated images are not accepted."""
    (build / shared.KERNEL_ELF).write_bytes(image)
    execute = Mock()
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    with pytest.raises(fake_gdb.GdbError, match=reason):
        load_rtos_project("zephyr", str(build))
    execute.assert_not_called()
    assert not rtos_status()["loaded"]


def test_transfer_staging_never_resolves_original_source_paths(
    build: Path, tmp_path: Path, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete staged build can be activated without its original workspace."""
    store = RtosProjects(tmp_path)
    execute = Mock(return_value="")
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    try:
        store.begin("demo", "zephyr")
        for relative in shared.REQUIRED:
            content = (build / relative).read_bytes()
            store.put("demo", relative, 0, len(content), base64.b64encode(content).decode())
        rtos, path = store.prepare("demo")
        assert path != build
        snapshot = load_rtos_project(rtos, str(path))
        store.finish("demo", True)
        assert snapshot["loaded"] and snapshot["project_path"] == str(path)
        assert snapshot["task_list"] is None
        assert not (path / "zephyr/zephyr_pre0.elf").exists()
        assert not (path / "compile_commands.json").exists()
    finally:
        store.close()


def test_build_context_requires_stopped_target_and_rejects_wrong_primary(
    build: Path, fake_gdb: ModuleType
) -> None:
    """Thread inspection needs a target; unsupported scheduler calls fail explicitly."""
    backend = zephyr.ZephyrRtos()
    project = backend.load_project(build, build / shared.KERNEL_ELF)
    with pytest.raises(fake_gdb.error, match="no inferior selected"):
        project.show_task("worker")
    with pytest.raises(ValueError, match="not implemented"):
        project.trace_scheduler(2, Console(file=StringIO()), Mock())
    with pytest.raises(ValueError, match="primary"):
        backend.load_project(build, build / "zephyr/zephyr_pre0.elf")
