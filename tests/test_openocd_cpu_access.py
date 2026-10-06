# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""OpenOCD external CPU identity and monitor/thread synchronization regressions."""

from types import SimpleNamespace

import pytest

from pyGdbToolkit.ocd.openocd import read_external_debug_word


def test_external_midr_uses_configured_dap_and_debug_base(fake_gdb, monkeypatch):
    """An arbitrary CPU name and base work without a SoC table or system memory access."""
    monkeypatch.setattr(
        fake_gdb, "selected_thread", lambda: SimpleNamespace(name="soc.cpu7"), raising=False
    )
    responses = {
        "monitor echo [target current]": "soc.cpu7",
        "monitor echo [list [soc.cpu7 cget -type] [soc.cpu7 cget -dbgbase] [soc.cpu7 cget -dap] [soc.cpu7 cget -ap-num]]": "aarch64 0x12340000 soc.dap 2",
        "monitor echo [target names]": "soc.cpu7 soc.debug",
        "monitor echo [soc.cpu7 cget -type]": "aarch64",
        "monitor echo [soc.debug cget -type]": "mem_ap",
        "monitor echo [list [soc.debug cget -dap] [soc.debug cget -ap-num]]": "soc.dap 2",
        "monitor echo [soc.debug read_memory 0x12340D00 32 1]": "0x410fd034",
    }
    calls = []

    def execute(command, to_string=True):
        calls.append(command)
        return responses[command]

    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    assert read_external_debug_word(0xD00) == 0x410FD034
    assert calls[-1] == "monitor echo [soc.debug read_memory 0x12340D00 32 1]"
    responses[calls[-1]] = "access denied 0x410fd034"
    assert read_external_debug_word(0xD00) is None
    responses["monitor echo [list [soc.debug cget -dap] [soc.debug cget -ap-num]]"] = "other.dap 2"
    calls.clear()
    assert read_external_debug_word(0xD00) is None
    assert not any("read_memory" in command for command in calls)


@pytest.mark.parametrize("name,current", (("soc.cpu1", "soc.cpu0"), ("soc.cpu1;reset", "soc.cpu1")))
def test_external_identity_rejects_wrong_context_or_unsafe_names(
    fake_gdb, monkeypatch, name, current
):
    """No memory reads occur when GDB and monitor target selection disagree."""
    monkeypatch.setattr(
        fake_gdb, "selected_thread", lambda: SimpleNamespace(name=name), raising=False
    )
    calls = []
    monkeypatch.setattr(
        fake_gdb,
        "execute",
        lambda command, **kwargs: calls.append(command) or current,
        raising=False,
    )
    assert read_external_debug_word(0xD00) is None
    assert calls in ([], ["monitor echo [target current]"])
