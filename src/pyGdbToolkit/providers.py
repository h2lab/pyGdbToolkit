"""Vendor device providers and the STM32 electronic-signature catalog.

The STM32 catalog is deliberately local and declarative.  Its addresses,
fixed SRAM totals, and factory flash fallbacks are derived from the official
ST CMSIS device headers listed in ``README.md``; the headers themselves are
not vendored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import CPUID, DeviceReport, FieldValue
from .target_memory import TargetMemory, TargetReadError

DBGMCU_IDCODE_F0_G0 = 0x40015800
DBGMCU_IDCODE_COMMON = 0xE0042000
DBGMCU_IDCODE_NEWER = 0xE0044000
DBGMCU_IDCODE_H5 = 0x44024000
DBGMCU_IDCODE_H7 = 0x5C001000
DBGMCU_IDCODE_N6 = 0x44001000

_FLASH_DEFAULT_16 = (0x0000, 0xFFFF)
_FLASH_DEFAULT_L4 = (0xFFFF,)


class DeviceProvider(Protocol):
    """A vendor-specific device recognizer."""

    def inspect(self, reader: TargetMemory, cpuid: CPUID) -> DeviceReport | None:
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
    """A documented STM32 product line identified by one or more DEV_ID values."""

    product_line: str
    device_ids: tuple[int, ...]
    idcode_address: int
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


STM32_PROFILES = (
    Stm32Profile("STM32C01xx", (0x443,), DBGMCU_IDCODE_F0_G0, _C01_SIGNATURE, 6),
    Stm32Profile("STM32C03xx", (0x453,), DBGMCU_IDCODE_F0_G0, _C03_SIGNATURE, 12),
    Stm32Profile("STM32C05xx", (0x44C,), DBGMCU_IDCODE_F0_G0, _C05_SIGNATURE, 12),
    Stm32Profile("STM32C071xx", (0x493,), DBGMCU_IDCODE_F0_G0, _C071_SIGNATURE, 24),
    Stm32Profile("STM32C09xx", (0x44D,), DBGMCU_IDCODE_F0_G0, _C09_SIGNATURE, 36),
    Stm32Profile("STM32C55xx/C56xx", (0x44E,), DBGMCU_IDCODE_H5, _C5_55_56_SIGNATURE, 128),
    Stm32Profile("STM32C53xx/C54xx", (0x44F,), DBGMCU_IDCODE_H5, _C5_53_54_SIGNATURE, 64),
    Stm32Profile("STM32C59xx/C5Axx", (0x45A,), DBGMCU_IDCODE_H5, _C5_59_5A_SIGNATURE, 256),
    Stm32Profile("STM32F03x", (0x444,), DBGMCU_IDCODE_F0_G0, _F0_SIGNATURE),
    Stm32Profile("STM32F04x", (0x445,), DBGMCU_IDCODE_F0_G0, _F0_SIGNATURE),
    Stm32Profile("STM32F05x", (0x440,), DBGMCU_IDCODE_F0_G0, _F0_SIGNATURE),
    Stm32Profile("STM32F07x", (0x448,), DBGMCU_IDCODE_F0_G0, _F0_SIGNATURE),
    Stm32Profile("STM32F09x", (0x442,), DBGMCU_IDCODE_F0_G0, _F0_SIGNATURE),
    Stm32Profile("STM32F10x medium-density", (0x410,), DBGMCU_IDCODE_COMMON, _F1_SIGNATURE),
    Stm32Profile("STM32F10x low-density", (0x412,), DBGMCU_IDCODE_COMMON, _F1_SIGNATURE),
    Stm32Profile("STM32F10x high-density", (0x414,), DBGMCU_IDCODE_COMMON, _F1_SIGNATURE),
    Stm32Profile("STM32F10x connectivity line", (0x418,), DBGMCU_IDCODE_COMMON, _F1_SIGNATURE),
    Stm32Profile(
        "STM32F100 low/medium-density value line",
        (0x420,),
        DBGMCU_IDCODE_COMMON,
        _F1_SIGNATURE,
    ),
    Stm32Profile(
        "STM32F100 high-density value line",
        (0x428,),
        DBGMCU_IDCODE_COMMON,
        _F1_SIGNATURE,
    ),
    Stm32Profile("STM32F10x XL-density", (0x430,), DBGMCU_IDCODE_COMMON, _F1_SIGNATURE),
    Stm32Profile("STM32F2xx", (0x411,), DBGMCU_IDCODE_COMMON, _F2_SIGNATURE),
    Stm32Profile(
        "STM32F3 product lines",
        (0x422, 0x432, 0x438, 0x439, 0x446),
        DBGMCU_IDCODE_COMMON,
        _F3_SIGNATURE,
    ),
    Stm32Profile(
        "STM32F405/F407/F415/F417",
        (0x413,),
        DBGMCU_IDCODE_COMMON,
        _F4_SIGNATURE,
    ),
    Stm32Profile("STM32F42x/F43x", (0x419,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F446", (0x421,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F401xB/xC", (0x423,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F411", (0x431,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F401xD/xE", (0x433,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F469/F479", (0x434,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F412", (0x441,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F410", (0x458,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F413/F423", (0x463,), DBGMCU_IDCODE_COMMON, _F4_SIGNATURE),
    Stm32Profile("STM32F72x/F73x", (0x452,), DBGMCU_IDCODE_COMMON, _F7_72_73_SIGNATURE),
    Stm32Profile("STM32F74x/F75x", (0x449,), DBGMCU_IDCODE_COMMON, _F7_74_77_SIGNATURE),
    Stm32Profile("STM32F76x/F77x", (0x451,), DBGMCU_IDCODE_COMMON, _F7_74_77_SIGNATURE),
    Stm32Profile("STM32G05x/G06x", (0x456,), DBGMCU_IDCODE_F0_G0, _G0_SIGNATURE, 16),
    Stm32Profile("STM32G07x/G08x", (0x460,), DBGMCU_IDCODE_F0_G0, _G0_SIGNATURE, 32),
    Stm32Profile("STM32G03x/G04x", (0x466,), DBGMCU_IDCODE_F0_G0, _G0_SIGNATURE, 8),
    Stm32Profile("STM32G0Bx/G0Cx", (0x467,), DBGMCU_IDCODE_F0_G0, _G0_SIGNATURE, 128),
    Stm32Profile("STM32G43x/G44x", (0x468,), DBGMCU_IDCODE_COMMON, _COMMON_SIGNATURE, 32),
    Stm32Profile("STM32G47x/G48x", (0x469,), DBGMCU_IDCODE_COMMON, _COMMON_SIGNATURE, 128),
    Stm32Profile("STM32G49x/G4Ax", (0x479,), DBGMCU_IDCODE_COMMON, _COMMON_SIGNATURE, 112),
    Stm32Profile("STM32H503", (0x474,), DBGMCU_IDCODE_H5, _H5_503_SIGNATURE, 32),
    Stm32Profile("STM32H52x/H53x", (0x478,), DBGMCU_IDCODE_H5, _H5_52_53_SIGNATURE, 272),
    Stm32Profile("STM32H5Ex/H5Fx", (0x47A,), DBGMCU_IDCODE_H5, _H5_E_F_SIGNATURE, 1536),
    Stm32Profile("STM32H54x/H55x", (0x47C,), DBGMCU_IDCODE_H5, _H5_SIGNATURE),
    Stm32Profile("STM32H56x/H57x", (0x484,), DBGMCU_IDCODE_H5, _H5_56_57_SIGNATURE, 640),
    Stm32Profile("STM32H74x/H75x", (0x450,), DBGMCU_IDCODE_H7, _H7_74_75_SIGNATURE),
    Stm32Profile("STM32H7Ax/H7Bx", (0x480,), DBGMCU_IDCODE_H7, _H7A_H7B_SIGNATURE),
    Stm32Profile("STM32H72x/H73x", (0x483,), DBGMCU_IDCODE_H7, _H7_72_73_SIGNATURE),
    Stm32Profile("STM32H7Rx/H7Sx", (0x485,), DBGMCU_IDCODE_H7, _H7RS_SIGNATURE),
    Stm32Profile("STM32H7Pxx", (0x47B,), DBGMCU_IDCODE_H7, SignatureLayout()),
    Stm32Profile(
        "STM32L0 category 1/2/3/5",
        (0x417, 0x425, 0x447, 0x457),
        DBGMCU_IDCODE_F0_G0,
        _L0_SIGNATURE,
    ),
    Stm32Profile(
        "STM32L1 product lines",
        (0x416, 0x427, 0x429, 0x436, 0x437),
        DBGMCU_IDCODE_COMMON,
        _L1_SIGNATURE,
    ),
    Stm32Profile("STM32L47x/L48x", (0x415,), DBGMCU_IDCODE_COMMON, _L4_47_48_SIGNATURE, 128),
    Stm32Profile("STM32L43x/L44x", (0x435,), DBGMCU_IDCODE_COMMON, _L4_43_44_SIGNATURE, 64),
    Stm32Profile("STM32L49x/L4Ax", (0x461,), DBGMCU_IDCODE_COMMON, _L4_49_4A_SIGNATURE, 320),
    Stm32Profile("STM32L45x/L46x", (0x462,), DBGMCU_IDCODE_COMMON, _L4_45_46_SIGNATURE, 160),
    Stm32Profile("STM32L41x/L42x", (0x464,), DBGMCU_IDCODE_COMMON, _L4_41_42_SIGNATURE, 40),
    Stm32Profile("STM32L4Rx/L4Sx", (0x470,), DBGMCU_IDCODE_COMMON, _L4R_4S_SIGNATURE, 640),
    Stm32Profile("STM32L4Px/L4Qx", (0x471,), DBGMCU_IDCODE_COMMON, _L4P_4Q_SIGNATURE, 320),
    Stm32Profile("STM32L55x/L56x", (0x472,), DBGMCU_IDCODE_NEWER, _L5_SIGNATURE, 256),
    Stm32Profile("STM32U031xx", (0x459,), DBGMCU_IDCODE_F0_G0, _U0_31_SIGNATURE, 8),
    Stm32Profile("STM32U073x/U083x", (0x489,), DBGMCU_IDCODE_F0_G0, _U0_73_83_SIGNATURE, 32),
    Stm32Profile("STM32U3B/U3Cxx", (0x42A,), DBGMCU_IDCODE_NEWER, _U3_B_C_SIGNATURE, 640),
    Stm32Profile("STM32U37x/U38x", (0x454,), DBGMCU_IDCODE_NEWER, _U3_37_38_SIGNATURE, 256),
    Stm32Profile("STM32U535x/U545x", (0x455,), DBGMCU_IDCODE_NEWER, _U5_53_54_SIGNATURE, 272),
    Stm32Profile("STM32U57x/U58x", (0x482,), DBGMCU_IDCODE_NEWER, _U5_57_58_SIGNATURE, 784),
    Stm32Profile(
        "STM32U595/U599/U5A5/U5A9",
        (0x481,),
        DBGMCU_IDCODE_NEWER,
        _U5_59_5A_5F_5G_SIGNATURE,
        2512,
    ),
    Stm32Profile("STM32U5Fx/U5Gx", (0x476,), DBGMCU_IDCODE_NEWER, _U5_59_5A_5F_5G_SIGNATURE, 3024),
    Stm32Profile("STM32WB1x", (0x494,), DBGMCU_IDCODE_COMMON, _WB_SIGNATURE, 48),
    Stm32Profile("STM32WB3x", (0x496,), DBGMCU_IDCODE_COMMON, _WB_SIGNATURE, 96),
    Stm32Profile("STM32WB5x", (0x495,), DBGMCU_IDCODE_COMMON, _WB_SIGNATURE, 256),
    Stm32Profile("STM32WBA2x", (0x4B2,), DBGMCU_IDCODE_NEWER, _WBA_2_5_SIGNATURE, 128),
    Stm32Profile("STM32WBA5x", (0x492,), DBGMCU_IDCODE_NEWER, _WBA_2_5_SIGNATURE, 128),
    Stm32Profile("STM32WBA6x", (0x4B0,), DBGMCU_IDCODE_NEWER, _WBA_6_SIGNATURE, 512),
    Stm32Profile("STM32WLE/WL5x", (0x497,), DBGMCU_IDCODE_COMMON, _WL_SIGNATURE, 64),
    Stm32Profile("STM32N6 product line", (0x486,), DBGMCU_IDCODE_N6, _N6_SIGNATURE, 4240),
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

    def inspect(self, reader: TargetMemory, cpuid: CPUID) -> DeviceReport | None:
        """Recognize an STM32 target through its documented DBGMCU IDCODE.

        Parameters
        ----------
        reader
            Typed reader for target memory.
        cpuid
            The previously decoded standard Arm CPUID register.

        Returns
        -------
        DeviceReport | None
            A report for a documented STM32 profile or ``None`` when no
            documented IDCODE matches.
        """
        for address in self._idcode_addresses():
            try:
                idcode = reader.read_uint32(address)
            except TargetReadError:
                continue
            device_id = idcode & 0xFFF
            matches = tuple(
                profile
                for profile in self._profiles
                if profile.idcode_address == address and device_id in profile.device_ids
            )
            if len(matches) == 1:
                return self._profile_report(reader, cpuid, matches[0], idcode)
            if len(matches) > 1:
                return self._ambiguous_report(cpuid, idcode, matches)
        return None

    def _idcode_addresses(self) -> tuple[int, ...]:
        """Return distinct documented IDCODE addresses in profile order."""
        addresses: list[int] = []
        for profile in self._profiles:
            if profile.idcode_address not in addresses:
                addresses.append(profile.idcode_address)
        return tuple(addresses)

    def _profile_report(
        self,
        reader: TargetMemory,
        cpuid: CPUID,
        profile: Stm32Profile,
        idcode: int,
    ) -> DeviceReport:
        """Build a report after exactly one documented STM32 profile matched."""
        return DeviceReport(
            cpuid=cpuid,
            vendor="STMicroelectronics",
            product_line=FieldValue.known(profile.product_line),
            idcode=FieldValue.known(_format_idcode(idcode)),
            part_number=FieldValue.known(
                f"{profile.product_line} (exact ordering code unavailable from DEV_ID)"
            ),
            ram=_ram_size(profile),
            flash=_read_flash_size(reader, profile.signature),
            package=_read_package(reader, profile.signature),
            serial_number=_read_serial_number(reader, profile.signature),
        )

    def _ambiguous_report(
        self,
        cpuid: CPUID,
        idcode: int,
        matches: tuple[Stm32Profile, ...],
    ) -> DeviceReport:
        """Build a safe report when one DEV_ID matches multiple profiles."""
        candidates = ", ".join(profile.product_line for profile in matches)
        reason = "DEV_ID is shared by multiple documented STM32 product lines"
        return DeviceReport(
            cpuid=cpuid,
            vendor="STMicroelectronics",
            product_line=FieldValue.known(f"Ambiguous: {candidates}"),
            idcode=FieldValue.known(_format_idcode(idcode)),
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

    def inspect(self, reader: TargetMemory, cpuid: CPUID) -> DeviceReport:
        """Return the first provider report or the generic Cortex-M report.

        Parameters
        ----------
        reader
            Typed reader for target memory.
        cpuid
            The decoded standard Arm CPUID register.

        Returns
        -------
        DeviceReport
            A vendor report or an explicit generic fallback.
        """
        for provider in self._providers:
            report = provider.inspect(reader, cpuid)
            if report is not None:
                return report
        return _generic_report(cpuid)


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
        "RAM size is SKU-dependent; DEV_ID does not select one fixed CMSIS SRAM map"
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


def _format_idcode(idcode: int) -> str:
    """Format the STM32 DBGMCU IDCODE with its documented component fields."""
    return f"0x{idcode:08X} (DEV_ID 0x{idcode & 0xFFF:03X}, REV_ID 0x{idcode >> 16:04X})"


def _generic_report(cpuid: CPUID) -> DeviceReport:
    """Return a useful report when no registered vendor device matched."""
    reason = "no registered vendor profile matched this Cortex-M target"
    return DeviceReport(
        cpuid=cpuid,
        vendor="Generic Cortex-M",
        product_line=FieldValue.unavailable(reason),
        idcode=FieldValue.unavailable(reason),
        part_number=FieldValue.unavailable(reason),
        ram=FieldValue.unavailable(reason),
        flash=FieldValue.unavailable(reason),
        package=FieldValue.unavailable(reason),
        serial_number=FieldValue.unavailable(reason),
    )


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry((Stm32Provider(),))
