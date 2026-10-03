"""Read-only ARM AP profile tests."""

from pyGdbToolkit.arch.arm.dap import ArmAccessPortProvider
from pyGdbToolkit.debug_port import AccessPort, DebugPortError


class Registers:
    def __init__(self, values: dict[int, int]) -> None:
        self.values = values
        self.reads = []

    def read_ap(self, index: int, address: int) -> int:
        self.reads.append((index, address))
        if address not in self.values:
            raise DebugPortError("unavailable")
        return self.values[address]


def test_mem_ap_identity_and_capabilities() -> None:
    transport = Registers({0xFC: 0x24770011, 0xF4: 0, 0: 0x42, 0xF8: 0xE00FF003})
    profile = ArmAccessPortProvider().profile(transport, AccessPort(0, "AHB-AP"))
    assert profile.type_name == "AHB-AP"
    assert profile.identity["designer"] == "ARM"
    assert profile.identity["revision"] == 2
    assert profile.capabilities["memory_access"] is True
    assert profile.capabilities["device_enabled"] is True
    assert profile.capabilities["rom_table_address"] == 0xE00FF000
    assert profile.capabilities["current_transfer_size_bits"] == 32
    assert not profile.errors


def test_optional_reads_remain_unknown() -> None:
    profile = ArmAccessPortProvider().profile(
        Registers({0xFC: 0x24770011}), AccessPort(0, "AHB-AP")
    )
    assert profile.capabilities["large_address"] is None
    assert set(profile.errors) == {"CFG", "CSW", "BASE"}


def test_non_memory_ap_does_not_read_memory_registers() -> None:
    transport = Registers({0xFC: 0x04760000})
    profile = ArmAccessPortProvider().profile(transport, AccessPort(3, "JTAG-AP"))
    assert profile.type_name == "JTAG-AP"
    assert profile.capabilities["memory_access"] is False
    assert transport.reads == [(3, 0xFC)]


def test_large_address_reads_base2() -> None:
    profile = ArmAccessPortProvider().profile(
        Registers({0xFC: 0x24770011, 0xF4: 6, 0: 0, 0xF8: 0x1003, 0xF0: 2}),
        AccessPort(0, "AHB-AP"),
    )
    assert profile.capabilities["rom_table_address"] == 0x200001000
    assert profile.capabilities["large_data"] is True


def test_missing_idr_keeps_error() -> None:
    profile = ArmAccessPortProvider().profile(Registers({}), AccessPort(0, "AHB-AP"))
    assert profile.type_name == "unknown"
    assert not profile.capabilities
    assert profile.errors == {"IDR": "unavailable"}


def test_rp2350_apv2_profile() -> None:
    transport = Registers(
        {0xDFC: 0x34770008, 0xDF4: 0x000101A0, 0xD00: 0x03800052, 0xDF8: 0xE00FF003}
    )
    profile = ArmAccessPortProvider().profile(transport, AccessPort(0x2000, "AHB5-AP", True, 2))
    assert profile.type_name == "AHB5-AP (extended HPROT)"
    assert profile.identity["ap_version"] == 2
    assert profile.capabilities["rom_table_address"] == 0xE00FF000
    assert not profile.errors
