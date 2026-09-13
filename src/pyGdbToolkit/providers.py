# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Vendor device providers and the STM32 electronic-signature catalog.

The STM32 catalog is deliberately local and declarative.  Its addresses,
fixed SRAM totals, and factory flash fallbacks are derived from the official
ST CMSIS device headers listed in ``README.md``; the headers themselves are
not vendored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .coresight import CoreSightDiscovery, Jep106Identity
from .models import CPUID, DeviceReport, FieldValue
from .target_memory import TargetMemory, TargetReadError

_FLASH_DEFAULT_16 = (0x0000, 0xFFFF)
_FLASH_DEFAULT_L4 = (0xFFFF,)


class DeviceProvider(Protocol):
    """A vendor-specific device recognizer."""

    def inspect(
        self,
        reader: TargetMemory,
        cpuid: CPUID,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport | None:
        """Return a report if the provider recognizes the target."""
        ...


@dataclass(frozen=True)
class SignatureLayout:
    """Documented electronic-signature addresses for one STM32 product line."""

    flash_size_address: int | None = None
    uid_address: int | None = None
    package_address: int | None = None
    flash_size_mask: int = 0xFFFF
    flash_size_default_kib: int | None = None
    flash_size_default_values: tuple[int, ...] = ()
    flash_size_fixed_kib: int | None = None
    package_codes: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Stm32Profile:
    """A documented STM32 product line keyed by validated MCU-ROM identity."""

    product_line: str
    jep106: Jep106Identity
    mcu_rom_part_numbers: tuple[int, ...]
    signature: SignatureLayout
    ram_kib: int | None = None


@dataclass(frozen=True)
class UnmappedStm32Signature:
    """CMSIS-derived STM32 signature layout and fixed RAM metadata."""

    product_line: str
    signature: SignatureLayout
    ram_kib: int | None = None


_F0_SIGNATURE = SignatureLayout(flash_size_address=0x1FFFF7CC, uid_address=0x1FFFF7AC)
_F1_SIGNATURE = SignatureLayout(flash_size_address=0x1FFFF7E0, uid_address=0x1FFFF7E8)
_F2_SIGNATURE = SignatureLayout(flash_size_address=0x1FFF7A22, uid_address=0x1FFF7A10)
_F3_SIGNATURE = SignatureLayout(flash_size_address=0x1FFFF7CC, uid_address=0x1FFFF7AC)
_F4_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF7A22,
    uid_address=0x1FFF7A10,
    package_address=0x1FFF7BF0,
)
_F7_72_73_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FF07A22,
    uid_address=0x1FF07A10,
    package_address=0x1FF07BF0,
)
_F7_74_77_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FF0F442,
    uid_address=0x1FF0F420,
    package_address=0x1FF0F7E0,
)
_C5_55_56_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=512,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C5_53_54_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=256,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C5_59_5A_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C01_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75A0,
    uid_address=0x1FFF7550,
    package_address=0x1FFF7500,
    flash_size_default_kib=32,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C03_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75A0,
    uid_address=0x1FFF7550,
    package_address=0x1FFF7500,
    flash_size_default_kib=32,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C05_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75A0,
    uid_address=0x1FFF7550,
    package_address=0x1FFF7500,
    flash_size_default_kib=64,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C071_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75A0,
    uid_address=0x1FFF7550,
    package_address=0x1FFF7500,
    flash_size_default_kib=128,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_C09_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75A0,
    uid_address=0x1FFF7550,
    package_address=0x1FFF7500,
    flash_size_default_kib=256,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_G0_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_mask=0x007F,
)
_COMMON_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
)
_H5_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
)
_H5_503_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=128,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H5_52_53_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=512,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H5_56_57_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H5_E_F_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_default_kib=4096,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H7_74_75_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FF1E880,
    uid_address=0x1FF1E800,
    flash_size_mask=0x0FFF,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H7_72_73_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FF1E880,
    uid_address=0x1FF1E800,
    flash_size_mask=0x0FFF,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H7A_H7B_SIGNATURE = SignatureLayout(
    flash_size_address=0x08FFF80C,
    uid_address=0x08FFF800,
    package_address=0x08FFF80E,
    flash_size_mask=0x0FFF,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_H7RS_SIGNATURE = SignatureLayout(
    uid_address=0x08FFF800,
    package_address=0x08FFF80C,
    flash_size_fixed_kib=64,
)
_L0_SIGNATURE = SignatureLayout(flash_size_address=0x1FF8007C, uid_address=0x1FF80050)
_L1_SIGNATURE = SignatureLayout(flash_size_address=0x1FF8004C, uid_address=0x1FF80050)
_L4_41_42_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=128,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L4_43_44_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=256,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L4_45_46_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=512,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L4_47_48_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L4_49_4A_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L4R_4S_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L4P_4Q_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_L4,
)
_L5_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA05E0,
    uid_address=0x0BFA0590,
    package_address=0x0BFA0500,
    flash_size_mask=0x0FFF,
    flash_size_default_kib=512,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U0_31_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF3EA0,
    uid_address=0x1FFF3E50,
    package_address=0x1FFF3D00,
    flash_size_default_kib=64,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U0_73_83_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF6EA0,
    uid_address=0x1FFF6E50,
    package_address=0x1FFF6D00,
    flash_size_default_kib=256,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U3_37_38_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA07A0,
    uid_address=0x0BFA0700,
    package_address=0x0BFA0500,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U3_B_C_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA07A0,
    uid_address=0x0BFA0700,
    package_address=0x0BFA0500,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U5_53_54_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA07A0,
    uid_address=0x0BFA0700,
    package_address=0x0BFA0500,
    flash_size_default_kib=512,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U5_57_58_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA07A0,
    uid_address=0x0BFA0700,
    package_address=0x0BFA0500,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_U5_59_5A_5F_5G_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA07A0,
    uid_address=0x0BFA0700,
    package_address=0x0BFA0500,
    flash_size_default_kib=4096,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_WB_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
    flash_size_mask=0x07FF,
)
_WBA_2_5_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BF907A0,
    uid_address=0x0BF90700,
    package_address=0x0BF90500,
    flash_size_default_kib=1024,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_WBA_6_SIGNATURE = SignatureLayout(
    flash_size_address=0x0BFA07A0,
    uid_address=0x0BFA0700,
    package_address=0x0BFA0500,
    flash_size_default_kib=2048,
    flash_size_default_values=_FLASH_DEFAULT_16,
)
_N6_SIGNATURE = SignatureLayout(uid_address=0x46009014)
_WL_SIGNATURE = SignatureLayout(
    flash_size_address=0x1FFF75E0,
    uid_address=0x1FFF7590,
    package_address=0x1FFF7500,
)


ST_JEP106_IDENTITY = Jep106Identity(bank=0, code=0x20)


_STM32_ROM_PART_NUMBERS = {
    "STM32C01xx": (0x443,),
    "STM32C03xx": (0x453,),
    "STM32C05xx": (0x44C,),
    "STM32C071xx": (0x493,),
    "STM32C09xx": (0x44D,),
    "STM32C55xx/C56xx": (0x44E,),
    "STM32C53xx/C54xx": (0x44F,),
    "STM32C59xx/C5Axx": (0x45A,),
    "STM32F03x": (0x444,),
    "STM32F04x": (0x445,),
    "STM32F05x": (0x440,),
    "STM32F07x": (0x448,),
    "STM32F09x": (0x442,),
    "STM32F10x medium-density": (0x410,),
    "STM32F10x low-density": (0x412,),
    "STM32F10x high-density": (0x414,),
    "STM32F10x connectivity line": (0x418,),
    "STM32F100 low/medium-density value line": (0x420,),
    "STM32F100 high-density value line": (0x428,),
    "STM32F10x XL-density": (0x430,),
    "STM32F2xx": (0x411,),
    "STM32F3 product lines": (0x422, 0x432, 0x438, 0x439, 0x446),
    "STM32F405/F407/F415/F417": (0x413,),
    "STM32F42x/F43x": (0x419,),
    "STM32F446": (0x421,),
    "STM32F401xB/xC": (0x423,),
    "STM32F411": (0x431,),
    "STM32F401xD/xE": (0x433,),
    "STM32F469/F479": (0x434,),
    "STM32F412": (0x441,),
    "STM32F410": (0x458,),
    "STM32F413/F423": (0x463,),
    "STM32F72x/F73x": (0x452,),
    "STM32F74x/F75x": (0x449,),
    "STM32F76x/F77x": (0x451,),
    "STM32G05x/G06x": (0x456,),
    "STM32G07x/G08x": (0x460,),
    "STM32G03x/G04x": (0x466,),
    "STM32G0Bx/G0Cx": (0x467,),
    "STM32G43x/G44x": (0x468,),
    "STM32G47x/G48x": (0x469,),
    "STM32G49x/G4Ax": (0x479,),
    "STM32H503": (0x474,),
    "STM32H52x/H53x": (0x478,),
    "STM32H5Ex/H5Fx": (0x47A,),
    "STM32H54x/H55x": (0x47C,),
    "STM32H56x/H57x": (0x484,),
    "STM32H74x/H75x": (0x450,),
    "STM32H7Ax/H7Bx": (0x480,),
    "STM32H72x/H73x": (0x483,),
    "STM32H7Rx/H7Sx": (0x485,),
    "STM32H7Pxx": (0x47B,),
    "STM32L0 category 1/2/3/5": (0x417, 0x425, 0x447, 0x457),
    "STM32L1 product lines": (0x416, 0x427, 0x429, 0x436, 0x437),
    "STM32L47x/L48x": (0x415,),
    "STM32L43x/L44x": (0x435,),
    "STM32L49x/L4Ax": (0x461,),
    "STM32L45x/L46x": (0x462,),
    "STM32L41x/L42x": (0x464,),
    "STM32L4Rx/L4Sx": (0x470,),
    "STM32L4Px/L4Qx": (0x471,),
    "STM32L55x/L56x": (0x472,),
    "STM32N6 product line": (0x486,),
    "STM32U031xx": (0x459,),
    "STM32U073x/U083x": (0x489,),
    "STM32U3B/U3Cxx": (0x42A,),
    "STM32U37x/U38x": (0x454,),
    "STM32U535x/U545x": (0x455,),
    "STM32U57x/U58x": (0x482,),
    "STM32U595/U599/U5A5/U5A9": (0x481,),
    "STM32U5Fx/U5Gx": (0x476,),
    "STM32WB1x": (0x494,),
    "STM32WB3x": (0x496,),
    "STM32WB5x": (0x495,),
    "STM32WBA2x": (0x4B2,),
    "STM32WBA5x": (0x492,),
    "STM32WBA6x": (0x4B0,),
    "STM32WLE/WL5x": (0x497,),
}

STM32_SIGNATURE_CATALOG = (
    UnmappedStm32Signature("STM32C01xx", _C01_SIGNATURE, 6),
    UnmappedStm32Signature("STM32C03xx", _C03_SIGNATURE, 12),
    UnmappedStm32Signature("STM32C05xx", _C05_SIGNATURE, 12),
    UnmappedStm32Signature("STM32C071xx", _C071_SIGNATURE, 24),
    UnmappedStm32Signature("STM32C09xx", _C09_SIGNATURE, 36),
    UnmappedStm32Signature("STM32C55xx/C56xx", _C5_55_56_SIGNATURE, 128),
    UnmappedStm32Signature("STM32C53xx/C54xx", _C5_53_54_SIGNATURE, 64),
    UnmappedStm32Signature("STM32C59xx/C5Axx", _C5_59_5A_SIGNATURE, 256),
    UnmappedStm32Signature("STM32F03x", _F0_SIGNATURE),
    UnmappedStm32Signature("STM32F04x", _F0_SIGNATURE),
    UnmappedStm32Signature("STM32F05x", _F0_SIGNATURE),
    UnmappedStm32Signature("STM32F07x", _F0_SIGNATURE),
    UnmappedStm32Signature("STM32F09x", _F0_SIGNATURE),
    UnmappedStm32Signature("STM32F10x medium-density", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F10x low-density", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F10x high-density", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F10x connectivity line", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F100 low/medium-density value line", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F100 high-density value line", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F10x XL-density", _F1_SIGNATURE),
    UnmappedStm32Signature("STM32F2xx", _F2_SIGNATURE),
    UnmappedStm32Signature("STM32F3 product lines", _F3_SIGNATURE),
    UnmappedStm32Signature("STM32F405/F407/F415/F417", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F42x/F43x", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F446", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F401xB/xC", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F411", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F401xD/xE", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F469/F479", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F412", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F410", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F413/F423", _F4_SIGNATURE),
    UnmappedStm32Signature("STM32F72x/F73x", _F7_72_73_SIGNATURE),
    UnmappedStm32Signature("STM32F74x/F75x", _F7_74_77_SIGNATURE),
    UnmappedStm32Signature("STM32F76x/F77x", _F7_74_77_SIGNATURE),
    UnmappedStm32Signature("STM32G05x/G06x", _G0_SIGNATURE, 16),
    UnmappedStm32Signature("STM32G07x/G08x", _G0_SIGNATURE, 32),
    UnmappedStm32Signature("STM32G03x/G04x", _G0_SIGNATURE, 8),
    UnmappedStm32Signature("STM32G0Bx/G0Cx", _G0_SIGNATURE, 128),
    UnmappedStm32Signature("STM32G43x/G44x", _COMMON_SIGNATURE, 32),
    UnmappedStm32Signature("STM32G47x/G48x", _COMMON_SIGNATURE, 128),
    UnmappedStm32Signature("STM32G49x/G4Ax", _COMMON_SIGNATURE, 112),
    UnmappedStm32Signature("STM32H503", _H5_503_SIGNATURE, 32),
    UnmappedStm32Signature("STM32H52x/H53x", _H5_52_53_SIGNATURE, 272),
    UnmappedStm32Signature("STM32H5Ex/H5Fx", _H5_E_F_SIGNATURE, 1536),
    UnmappedStm32Signature("STM32H54x/H55x", _H5_SIGNATURE),
    UnmappedStm32Signature("STM32H56x/H57x", _H5_56_57_SIGNATURE, 640),
    UnmappedStm32Signature("STM32H74x/H75x", _H7_74_75_SIGNATURE),
    UnmappedStm32Signature("STM32H7Ax/H7Bx", _H7A_H7B_SIGNATURE),
    UnmappedStm32Signature("STM32H72x/H73x", _H7_72_73_SIGNATURE),
    UnmappedStm32Signature("STM32H7Rx/H7Sx", _H7RS_SIGNATURE),
    UnmappedStm32Signature("STM32H7Pxx", SignatureLayout()),
    UnmappedStm32Signature("STM32L0 category 1/2/3/5", _L0_SIGNATURE),
    UnmappedStm32Signature("STM32L1 product lines", _L1_SIGNATURE),
    UnmappedStm32Signature("STM32L47x/L48x", _L4_47_48_SIGNATURE, 128),
    UnmappedStm32Signature("STM32L43x/L44x", _L4_43_44_SIGNATURE, 64),
    UnmappedStm32Signature("STM32L49x/L4Ax", _L4_49_4A_SIGNATURE, 320),
    UnmappedStm32Signature("STM32L45x/L46x", _L4_45_46_SIGNATURE, 160),
    UnmappedStm32Signature("STM32L41x/L42x", _L4_41_42_SIGNATURE, 40),
    UnmappedStm32Signature("STM32L4Rx/L4Sx", _L4R_4S_SIGNATURE, 640),
    UnmappedStm32Signature("STM32L4Px/L4Qx", _L4P_4Q_SIGNATURE, 320),
    UnmappedStm32Signature("STM32L55x/L56x", _L5_SIGNATURE, 256),
    UnmappedStm32Signature("STM32N6 product line", _N6_SIGNATURE, 4240),
    UnmappedStm32Signature("STM32U031xx", _U0_31_SIGNATURE, 8),
    UnmappedStm32Signature("STM32U073x/U083x", _U0_73_83_SIGNATURE, 32),
    UnmappedStm32Signature("STM32U3B/U3Cxx", _U3_B_C_SIGNATURE, 640),
    UnmappedStm32Signature("STM32U37x/U38x", _U3_37_38_SIGNATURE, 256),
    UnmappedStm32Signature("STM32U535x/U545x", _U5_53_54_SIGNATURE, 272),
    UnmappedStm32Signature("STM32U57x/U58x", _U5_57_58_SIGNATURE, 784),
    UnmappedStm32Signature("STM32U595/U599/U5A5/U5A9", _U5_59_5A_5F_5G_SIGNATURE, 2512),
    UnmappedStm32Signature("STM32U5Fx/U5Gx", _U5_59_5A_5F_5G_SIGNATURE, 3024),
    UnmappedStm32Signature("STM32WB1x", _WB_SIGNATURE, 48),
    UnmappedStm32Signature("STM32WB3x", _WB_SIGNATURE, 96),
    UnmappedStm32Signature("STM32WB5x", _WB_SIGNATURE, 256),
    UnmappedStm32Signature("STM32WBA2x", _WBA_2_5_SIGNATURE, 128),
    UnmappedStm32Signature("STM32WBA5x", _WBA_2_5_SIGNATURE, 128),
    UnmappedStm32Signature("STM32WBA6x", _WBA_6_SIGNATURE, 512),
    UnmappedStm32Signature("STM32WLE/WL5x", _WL_SIGNATURE, 64),
)

STM32_PROFILES = tuple(
    Stm32Profile(
        product_line=signature.product_line,
        jep106=ST_JEP106_IDENTITY,
        mcu_rom_part_numbers=_STM32_ROM_PART_NUMBERS[signature.product_line],
        signature=signature.signature,
        ram_kib=signature.ram_kib,
    )
    for signature in STM32_SIGNATURE_CATALOG
)


class Stm32Provider:
    """Recognize documented STM32 product lines and read their signatures."""

    def __init__(self, profiles: tuple[Stm32Profile, ...] = STM32_PROFILES) -> None:
        """Create a provider using an ordered immutable profile catalog.

        Parameters
        ----------
        profiles
            Product-line profiles to probe. This supports extensions and tests
            without changing global state.
        """
        self._profiles = profiles

    def inspect(
        self,
        reader: TargetMemory,
        cpuid: CPUID,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport | None:
        """Recognize an STM32 target from its validated MCU-ROM identity.

        Parameters
        ----------
        reader
            Typed reader for target memory.
        cpuid
            The previously decoded standard Arm CPUID register.
        discovery
            Best-effort discovery results containing the MCU-ROM root identity.

        Returns
        -------
        DeviceReport | None
            A report for a documented STM32 profile or ``None`` when no
            registered MCU-ROM identity matches.
        """
        table = discovery.mcu_rom.table
        if table is None:
            return None
        peripheral_id = table.identity.peripheral_id
        if peripheral_id.jep106 is None:
            return None
        matches = tuple(
            profile
            for profile in self._profiles
            if profile.jep106 == peripheral_id.jep106
            and peripheral_id.part_number in profile.mcu_rom_part_numbers
        )
        if len(matches) == 1:
            return self._profile_report(reader, cpuid, discovery, matches[0])
        if len(matches) > 1:
            return self._ambiguous_report(cpuid, discovery, matches)
        return None

    def _profile_report(
        self,
        reader: TargetMemory,
        cpuid: CPUID,
        discovery: CoreSightDiscovery,
        profile: Stm32Profile,
    ) -> DeviceReport:
        """Build a report after exactly one documented STM32 profile matched."""
        return DeviceReport(
            cpuid=cpuid,
            discovery=discovery,
            vendor="STMicroelectronics",
            product_line=FieldValue.known(profile.product_line),
            part_number=FieldValue.known(
                f"{profile.product_line} (exact ordering code unavailable from MCU-ROM part)"
            ),
            ram=_ram_size(profile),
            flash=_read_flash_size(reader, profile.signature),
            package=_read_package(reader, profile.signature),
            serial_number=_read_serial_number(reader, profile.signature),
        )

    def _ambiguous_report(
        self,
        cpuid: CPUID,
        discovery: CoreSightDiscovery,
        matches: tuple[Stm32Profile, ...],
    ) -> DeviceReport:
        """Build a safe report when one MCU-ROM identity matches multiple profiles."""
        candidates = ", ".join(profile.product_line for profile in matches)
        reason = "MCU-ROM identity is shared by multiple documented STM32 product lines"
        return DeviceReport(
            cpuid=cpuid,
            discovery=discovery,
            vendor="STMicroelectronics",
            product_line=FieldValue.known(f"Ambiguous: {candidates}"),
            part_number=FieldValue.unavailable(reason),
            ram=FieldValue.unavailable(reason),
            flash=FieldValue.unavailable(reason),
            package=FieldValue.unavailable(reason),
            serial_number=FieldValue.unavailable(reason),
        )


class ProviderRegistry:
    """Apply device providers in a defined order with a generic fallback."""

    def __init__(self, providers: tuple[DeviceProvider, ...]) -> None:
        """Create a registry.

        Parameters
        ----------
        providers
            Providers evaluated in tuple order.
        """
        self._providers = providers

    def inspect(
        self,
        reader: TargetMemory,
        cpuid: CPUID,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport:
        """Return the first provider report or the generic Cortex-M report.

        Parameters
        ----------
        reader
            Typed reader for target memory.
        cpuid
            The decoded standard Arm CPUID register.
        discovery
            Best-effort MCU and processor ROM-table discovery results.

        Returns
        -------
        DeviceReport
            A vendor report or an explicit generic fallback.
        """
        for provider in self._providers:
            report = provider.inspect(reader, cpuid, discovery)
            if report is not None:
                return report
        return _generic_report(cpuid, discovery)


def _read_flash_size(reader: TargetMemory, layout: SignatureLayout) -> FieldValue:
    """Read a CMSIS-documented STM32 flash capacity or fixed flash allocation."""
    if layout.flash_size_fixed_kib is not None:
        return FieldValue.known(f"{layout.flash_size_fixed_kib} KiB")
    if layout.flash_size_address is None:
        return FieldValue.unavailable(
            "no trusted flash-size signature is catalogued for this product line"
        )
    try:
        register_value = reader.read_uint16(layout.flash_size_address)
    except TargetReadError as error:
        return FieldValue.unavailable(
            f"could not read documented flash-size register ({error})"
        )
    if register_value in layout.flash_size_default_values:
        if layout.flash_size_default_kib is not None:
            return FieldValue.known(f"{layout.flash_size_default_kib} KiB")
        return FieldValue.unavailable(
            f"flash-size register reports 0x{register_value:X}; "
            "no product-line default is known"
        )
    raw_size = register_value & layout.flash_size_mask
    if raw_size in (0, layout.flash_size_mask):
        return FieldValue.unavailable(
            f"flash-size register reports 0x{raw_size:X}; an exact SKU would be required"
        )
    return FieldValue.known(f"{raw_size} KiB")


def _ram_size(profile: Stm32Profile) -> FieldValue:
    """Return a profile's fixed CMSIS SRAM capacity when it is unambiguous."""
    if profile.ram_kib is not None:
        return FieldValue.known(f"{profile.ram_kib} KiB")
    return FieldValue.unavailable(
        "RAM size is SKU-dependent; MCU-ROM part does not select one fixed CMSIS SRAM map"
    )


def _read_package(reader: TargetMemory, layout: SignatureLayout) -> FieldValue:
    """Read a documented package code without inventing a package-name mapping."""
    if layout.package_address is None:
        return FieldValue.unavailable(
            "no documented package register is catalogued for this product line"
        )
    try:
        package_code = reader.read_uint16(layout.package_address)
    except TargetReadError as error:
        return FieldValue.unavailable(
            f"could not read documented package register ({error})"
        )
    for code, name in layout.package_codes:
        if package_code == code:
            return FieldValue.known(name)
    return FieldValue.known(
        f"0x{package_code:04X} (raw package code; package type mapping unavailable)"
    )


def _read_serial_number(reader: TargetMemory, layout: SignatureLayout) -> FieldValue:
    """Read a documented 96-bit STM32 unique-device identifier."""
    if layout.uid_address is None:
        return FieldValue.unavailable(
            "no trusted 96-bit UID signature is catalogued for this product line"
        )
    try:
        words = tuple(
            reader.read_uint32(layout.uid_address + offset) for offset in range(0, 12, 4)
        )
    except TargetReadError as error:
        return FieldValue.unavailable(f"could not read documented 96-bit UID ({error})")
    return FieldValue.known(
        f"0x{words[0]:08X}{words[1]:08X}{words[2]:08X} (96-bit UID)"
    )


def _generic_report(cpuid: CPUID, discovery: CoreSightDiscovery) -> DeviceReport:
    """Return a useful report when no registered vendor device matched."""
    if discovery.mcu_rom.table is None:
        assert discovery.mcu_rom.unavailable_reason is not None
        reason = f"MCU-ROM identity unavailable: {discovery.mcu_rom.unavailable_reason}"
    else:
        peripheral_id = discovery.mcu_rom.table.identity.peripheral_id
        if peripheral_id.jep106 is None:
            reason = "MCU-ROM root does not advertise a JEDEC manufacturer identity"
        else:
            reason = (
                "no registered vendor profile matches MCU-ROM "
                f"{peripheral_id.jep106.display()}, part 0x{peripheral_id.part_number:03X}"
            )
    return DeviceReport(
        cpuid=cpuid,
        discovery=discovery,
        vendor="Generic Cortex-M",
        product_line=FieldValue.unavailable(reason),
        part_number=FieldValue.unavailable(reason),
        ram=FieldValue.unavailable(reason),
        flash=FieldValue.unavailable(reason),
        package=FieldValue.unavailable(reason),
        serial_number=FieldValue.unavailable(reason),
    )


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry((Stm32Provider(),))
