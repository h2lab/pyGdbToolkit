"""CMSIS-SVD download, parsing, and dictionary mapping primitives.

This module provides facilities to:
- Resolve and download SVD (System View Description) XML files from the
  cmsis-svd-data repository (https://github.com/cmsis-svd/cmsis-svd-data).
- Automatically detect the active SoC/MCU using cmd_lscpu target inspection
  and select the appropriate SVD file using a multi-vendor matching engine.
- Provide primitives to load SVD files explicitly by vendor/name or file path.
- Parse SVD XML definitions into rich immutable data models and plain Python
  dictionaries for peripheral, register, and bit-field inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from .cpuid import CPUID_ADDRESS, decode_cpuid
from .models import CPUID, DeviceReport
from .providers import DEFAULT_PROVIDER_REGISTRY
from .target_memory import TargetMemory, TargetMemoryReader, TargetReadError

CMSIS_SVD_DATA_RAW_URL = "https://raw.githubusercontent.com/cmsis-svd/cmsis-svd-data/main/data"

# Normalization mapping from CPU/Device vendor names to cmsis-svd-data folder names
VENDOR_DIR_MAPPING: dict[str, str] = {
    "stmicroelectronics": "STMicro",
    "stmicro": "STMicro",
    "stm": "STMicro",
    "st": "STMicro",
    "nxp semiconductors": "NXP",
    "nxp": "NXP",
    "freescale": "Freescale",
    "atmel": "Atmel",
    "microchip": "Atmel",
    "nordic semiconductor": "Nordic",
    "nordic": "Nordic",
    "raspberry pi": "RaspberryPi",
    "raspberrypi": "RaspberryPi",
    "raspberry": "RaspberryPi",
    "silicon labs": "SiliconLabs",
    "silabs": "SiliconLabs",
    "espressif": "Espressif",
    "texas instruments": "TexasInstruments",
    "ti": "TexasInstruments",
    "infineon": "Infineon",
    "gigadevice": "GigaDevice",
    "renesas": "Renesas",
    "cypress": "Cypress",
    "fujitsu": "Fujitsu",
    "holtek": "Holtek",
    "nuvoton": "Nuvoton",
    "toshiba": "Toshiba",
    "alifsemi": "AlifSemi",
    "arterytek": "ArteryTek",
}


class SvdError(Exception):
    """Base exception for all SVD related operations."""


class SvdNotFoundError(SvdError):
    """Raised when an SVD file cannot be located for the target device."""


class SvdDownloadError(SvdError):
    """Raised when downloading an SVD file fails."""


class SvdParseError(SvdError):
    """Raised when parsing an SVD XML document fails."""


@dataclass(frozen=True)
class SvdField:
    """Description of a register bit-field."""

    name: str
    description: str
    bit_offset: int
    bit_width: int
    access: str | None = None
    reset_value: int | None = None

    @property
    def bit_mask(self) -> int:
        """Return the bitmask corresponding to this field."""
        return ((1 << self.bit_width) - 1) << self.bit_offset

    def extract_value(self, reg_value: int) -> int:
        """Extract this field's integer value from a raw register word.

        Parameters
        ----------
        reg_value : int
            The full integer value of the parent register.

        Returns
        -------
        int
            The extracted and shifted bitfield value.
        """
        return (reg_value >> self.bit_offset) & ((1 << self.bit_width) - 1)

    def to_dict(self) -> dict[str, Any]:
        """Convert the field model to a plain dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the field.
        """
        return {
            "name": self.name,
            "description": self.description,
            "bit_offset": self.bit_offset,
            "bit_width": self.bit_width,
            "bit_mask": self.bit_mask,
            "access": self.access,
            "reset_value": self.reset_value,
        }


@dataclass(frozen=True)
class SvdRegister:
    """Description of a memory-mapped peripheral register."""

    name: str
    display_name: str | None
    description: str
    address_offset: int
    size: int
    access: str | None
    reset_value: int | None
    reset_mask: int | None
    fields: tuple[SvdField, ...] = ()

    def get_field(self, name: str) -> SvdField | None:
        """Find a field by case-insensitive name.

        Parameters
        ----------
        name : str
            The name of the field to find.

        Returns
        -------
        SvdField | None
            The matching field, or None if not found.
        """
        target = name.upper()
        for field_obj in self.fields:
            if field_obj.name.upper() == target:
                return field_obj
        return None

    def decode(self, value: int) -> list[dict[str, Any]]:
        """Decode a numeric register value into its constituent bitfields.

        Parameters
        ----------
        value : int
            The raw numeric value read from target memory.

        Returns
        -------
        list[dict[str, Any]]
            List of decoded field dictionaries containing extracted values.
        """
        results: list[dict[str, Any]] = []
        for field_obj in self.fields:
            extracted = field_obj.extract_value(value)
            results.append(
                {
                    "name": field_obj.name,
                    "description": field_obj.description,
                    "bit_offset": field_obj.bit_offset,
                    "bit_width": field_obj.bit_width,
                    "bit_mask": field_obj.bit_mask,
                    "value": extracted,
                    "hex_value": hex(extracted),
                    "access": field_obj.access,
                }
            )
        return results

    def to_dict(self) -> dict[str, Any]:
        """Convert the register model to a plain dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the register and all fields.
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "address_offset": self.address_offset,
            "size": self.size,
            "access": self.access,
            "reset_value": self.reset_value,
            "reset_mask": self.reset_mask,
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass(frozen=True)
class SvdPeripheral:
    """Description of a memory-mapped peripheral."""

    name: str
    description: str
    group_name: str | None
    base_address: int
    derived_from: str | None = None
    registers: tuple[SvdRegister, ...] = ()

    def get_register(self, name_or_offset: str | int) -> SvdRegister | None:
        """Find a register by case-insensitive name or address offset.

        Parameters
        ----------
        name_or_offset : str | int
            Register name (str) or offset in bytes (int).

        Returns
        -------
        SvdRegister | None
            The matching register, or None if not found.
        """
        if isinstance(name_or_offset, int):
            for reg in self.registers:
                if reg.address_offset == name_or_offset:
                    return reg
            return None

        target = name_or_offset.upper()
        for reg in self.registers:
            if reg.name.upper() == target:
                return reg
        return None

    def to_dict(self) -> dict[str, Any]:
        """Convert the peripheral model to a plain dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the peripheral and all registers.
        """
        return {
            "name": self.name,
            "description": self.description,
            "group_name": self.group_name,
            "base_address": self.base_address,
            "derived_from": self.derived_from,
            "registers": [r.to_dict() for r in self.registers],
        }


@dataclass(frozen=True)
class SvdDevice:
    """Complete description of an MCU peripheral and register map."""

    name: str
    version: str | None
    description: str
    vendor: str | None
    address_unit_bits: int
    width: int
    size: int
    reset_value: int
    reset_mask: int
    peripherals: tuple[SvdPeripheral, ...] = ()

    def get_peripheral(self, name_or_address: str | int) -> SvdPeripheral | None:
        """Find a peripheral by case-insensitive name or base address.

        Parameters
        ----------
        name_or_address : str | int
            Peripheral name (str) or base address (int).

        Returns
        -------
        SvdPeripheral | None
            The matching peripheral, or None if not found.
        """
        if isinstance(name_or_address, int):
            for periph in self.peripherals:
                if periph.base_address == name_or_address:
                    return periph
            return None

        target = name_or_address.upper()
        for periph in self.peripherals:
            if periph.name.upper() == target:
                return periph
        return None

    def find_peripheral_by_address(
        self, address: int
    ) -> tuple[SvdPeripheral, SvdRegister | None, int] | None:
        """Find a peripheral and matching register containing a given memory address.

        Parameters
        ----------
        address : int
            Target memory address.

        Returns
        -------
        tuple[SvdPeripheral, SvdRegister | None, int] | None
            Tuple of (peripheral, register_or_none, offset_within_peripheral),
            or None if no peripheral spans the given address.
        """
        for periph in self.peripherals:
            if periph.base_address <= address < periph.base_address + 0x10000:
                offset = address - periph.base_address
                reg = periph.get_register(offset)
                return periph, reg, offset
        return None

    def to_dict(self) -> dict[str, Any]:
        """Convert the entire device model to a plain nested dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the entire device hierarchy.
        """
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "vendor": self.vendor,
            "address_unit_bits": self.address_unit_bits,
            "width": self.width,
            "size": self.size,
            "reset_value": self.reset_value,
            "reset_mask": self.reset_mask,
            "peripherals": {p.name: p.to_dict() for p in self.peripherals},
        }


def detect_target_device(reader: TargetMemory | None = None) -> DeviceReport:
    """Detect the active SoC/MCU by reading CPUID and inspecting providers.

    Parameters
    ----------
    reader : TargetMemory | None
        Typed reader for target memory. When None, a new
        TargetMemoryReader is used.

    Returns
    -------
    DeviceReport
        The detected target device report.

    Raises
    ------
    SvdError
        If reading target memory fails.
    """
    if reader is None:
        reader = TargetMemoryReader()

    try:
        raw_cpuid = reader.read_uint32(CPUID_ADDRESS)
    except TargetReadError as error:
        raise SvdError(f"Cannot read target CPUID register: {error}") from error

    cpuid = decode_cpuid(raw_cpuid)
    return DEFAULT_PROVIDER_REGISTRY.inspect(reader, cpuid)


def resolve_svd_vendor(vendor_name: str) -> str:
    """Normalize a vendor string to match cmsis-svd-data directory structure.

    Parameters
    ----------
    vendor_name : str
        Raw vendor name (e.g. STMicroelectronics, Nordic Semiconductor).

    Returns
    -------
    str
        Corresponding directory name in the cmsis-svd-data/data repository.
    """
    normalized = vendor_name.strip().lower()
    return VENDOR_DIR_MAPPING.get(normalized, vendor_name.strip())


def _clean_soc_name(raw_name: str) -> list[str]:
    """Clean and extract potential SoC model tokens from a string.

    Parameters
    ----------
    raw_name : str
        Raw SoC or product line string.

    Returns
    -------
    list[str]
        Cleaned tokens suitable for candidate generation.
    """
    cleaned = re.sub(r"\(.*?\)", "", raw_name).strip()
    raw_parts = re.split(r"[/,]", cleaned)
    tokens: list[str] = []

    for part in raw_parts:
        token = part.strip()
        token = re.sub(
            r"(product line|series|line|medium-density|low-density|high-density|connectivity)",
            "",
            token,
            flags=re.IGNORECASE,
        ).strip()
        if token and token not in tokens:
            tokens.append(token)

    return tokens


def get_candidate_svd_filenames(
    product_line: str | None = None,
    part_number: str | None = None,
) -> list[str]:
    """Generate prioritized SVD filename candidates from SoC naming info.

    Parameters
    ----------
    product_line : str | None
        Product line identifier from device report (e.g. STM32F401, nRF52840).
    part_number : str | None
        Optional detailed part number.

    Returns
    -------
    list[str]
        List of candidate SVD filenames in order of preference.
    """
    raw_inputs: list[str] = []
    if part_number:
        raw_inputs.extend(_clean_soc_name(part_number))
    if product_line:
        raw_inputs.extend(_clean_soc_name(product_line))

    candidates: list[str] = []

    def add_candidate(name: str) -> None:
        if not name.lower().endswith(".svd"):
            name = f"{name}.svd"
        if name not in candidates:
            candidates.append(name)

    for token in raw_inputs:
        base = token.strip()
        if not base:
            continue

        add_candidate(base)
        add_candidate(base.lower())
        add_candidate(base.upper())

        trimmed_x = re.sub(r"[xX]+$", "", base)
        if trimmed_x:
            add_candidate(f"{trimmed_x}xx")
            add_candidate(f"{trimmed_x}x")
            add_candidate(f"{trimmed_x}xx".lower())
            add_candidate(f"{trimmed_x}xx".upper())
            add_candidate(f"{trimmed_x}x".lower())
            add_candidate(f"{trimmed_x}x".upper())
            add_candidate(trimmed_x)

        m = re.match(r"^([A-Za-z]+)(\d+)([A-Za-z0-9]*)", base)
        if m:
            prefix, digits, _ = m.group(1), m.group(2), m.group(3)
            add_candidate(f"{prefix}{digits}")
            add_candidate(f"{prefix}{digits}x")
            add_candidate(f"{prefix}{digits}xx")
            add_candidate(f"{prefix}{digits}".lower())
            add_candidate(f"{prefix}{digits}".upper())

            if len(digits) >= 3:
                short_digits = digits[:2]
                add_candidate(f"{prefix}{short_digits}")
                add_candidate(f"{prefix}{short_digits}xx")
                add_candidate(f"{prefix}{short_digits}x")
                add_candidate(f"{prefix}{short_digits}".lower())

    return candidates


def resolve_svd_candidates(report: DeviceReport) -> tuple[str, list[str]]:
    """Determine the vendor folder and candidate SVD filenames for a device report.

    Parameters
    ----------
    report : DeviceReport
        The device report obtained from cmd_lscpu target inspection.

    Returns
    -------
    tuple[str, list[str]]
        Tuple containing (vendor_dir, list_of_candidates).

    Raises
    ------
    SvdNotFoundError
        If the vendor is unknown or no product line is available.
    """
    if not report.vendor or report.vendor == "Unknown":
        raise SvdNotFoundError("Cannot determine SVD: vendor is unknown or unsupported.")

    vendor_dir = resolve_svd_vendor(report.vendor)
    product_line = report.product_line.value if report.product_line.is_available else ""
    part_number = report.part_number.value if report.part_number.is_available else None

    if not product_line and not part_number:
        raise SvdNotFoundError(
            f"Cannot determine SVD: no product line or part number identified for {report.vendor}."
        )

    candidates = get_candidate_svd_filenames(product_line, part_number)
    if not candidates:
        raise SvdNotFoundError(
            f"Cannot find candidate SVD filenames for {report.vendor} {product_line}."
        )

    return vendor_dir, candidates


def get_cache_dir(custom_path: Path | str | None = None) -> Path:
    """Return the base local directory used to cache downloaded SVD files.

    Parameters
    ----------
    custom_path : Path | str | None
        Optional explicit cache path override.

    Returns
    -------
    Path
        Path to the local SVD cache directory.
    """
    if custom_path is not None:
        cache_path = Path(custom_path)
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
        cache_path = base / "pyGdbToolkit" / "svd"

    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


def download_file(url: str, destination: Path, timeout: float = 15.0) -> Path:
    """Download a remote file over HTTP/HTTPS to a destination path.

    Parameters
    ----------
    url : str
        Remote URL of the file to fetch.
    destination : Path
        Local destination path.
    timeout : float
        Network timeout in seconds.

    Returns
    -------
    Path
        The destination file path.

    Raises
    ------
    SvdDownloadError
        If the download fails due to network error, HTTP error, or timeout.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "pyGdbToolkit-SvdFetcher/1.0",
                "Accept": "application/xml,text/xml,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content = response.read()
            destination.write_bytes(content)
        return destination
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SvdDownloadError(f"Failed to download {url}: {error}") from error


def fetch_svd_file(
    vendor_dir: str,
    filename: str,
    cache_dir: Path | None = None,
    force_download: bool = False,
    timeout: float = 15.0,
) -> Path:
    """Fetch an SVD file from local cache or download it from cmsis-svd-data.

    Parameters
    ----------
    vendor_dir : str
        Vendor directory name in cmsis-svd-data (e.g. STMicro, Nordic).
    filename : str
        SVD filename (e.g. STM32F401.svd, nrf52840.svd).
    cache_dir : Path | None
        Local cache directory. Defaults to standard user cache.
    force_download : bool
        Whether to bypass existing cache and re-download.
    timeout : float
        HTTP request timeout in seconds.

    Returns
    -------
    Path
        Path to the locally cached SVD file.

    Raises
    ------
    SvdDownloadError
        If download fails.
    """
    if not filename.lower().endswith(".svd"):
        filename = f"{filename}.svd"

    cache = get_cache_dir(cache_dir)
    local_path = cache / vendor_dir / filename

    if local_path.is_file() and not force_download:
        return local_path

    url = f"{CMSIS_SVD_DATA_RAW_URL}/{vendor_dir}/{filename}"
    return download_file(url, local_path, timeout=timeout)


def resolve_and_fetch_svd(
    report: DeviceReport,
    cache_dir: Path | None = None,
    force_download: bool = False,
    timeout: float = 15.0,
) -> tuple[Path, str]:
    """Resolve and fetch the best matching SVD file for a device report.

    Parameters
    ----------
    report : DeviceReport
        Device report from target inspection.
    cache_dir : Path | None
        Local cache directory.
    force_download : bool
        Whether to force re-downloading even if cached.
    timeout : float
        Network timeout in seconds.

    Returns
    -------
    tuple[Path, str]
        Tuple of (local_file_path, matched_filename).

    Raises
    ------
    SvdNotFoundError
        If no candidate SVD file could be located or downloaded.
    """
    vendor_dir, candidates = resolve_svd_candidates(report)
    cache = get_cache_dir(cache_dir)

    if not force_download:
        for candidate in candidates:
            cached_file = cache / vendor_dir / candidate
            if cached_file.is_file():
                return cached_file, candidate

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            downloaded = fetch_svd_file(
                vendor_dir,
                candidate,
                cache_dir=cache,
                force_download=force_download,
                timeout=timeout,
            )
            return downloaded, candidate
        except SvdDownloadError as err:
            last_error = err
            continue

    raise SvdNotFoundError(
        f"Could not locate or download SVD for {report.vendor} "
        f"candidates {candidates}. Last error: {last_error}"
    )


def _parse_int(text: str | None, default: int = 0) -> int:
    """Parse an integer from an SVD XML string (hex, decimal, or binary).

    Parameters
    ----------
    text : str | None
        String text from XML element.
    default : int
        Default value if text is empty or None.

    Returns
    -------
    int
        Parsed integer value.
    """
    if text is None:
        return default
    text = text.strip()
    if not text:
        return default
    if text.startswith("#"):
        return int(text[1:], 16)
    if text.startswith(("0x", "0X")):
        return int(text, 16)
    if text.startswith(("0b", "0B")):
        return int(text, 2)
    return int(text, 10)


def _parse_bit_range(field_elem: ET.Element) -> tuple[int, int]:
    """Parse bit offset and width from an SVD field element.

    Parameters
    ----------
    field_elem : ET.Element
        XML Element representing an SVD <field>.

    Returns
    -------
    tuple[int, int]
        Tuple of (bit_offset, bit_width).
    """
    offset_elem = field_elem.find("bitOffset")
    width_elem = field_elem.find("bitWidth")
    if offset_elem is not None and offset_elem.text:
        bit_offset = _parse_int(offset_elem.text)
        bit_width = (
            _parse_int(width_elem.text, default=1)
            if width_elem is not None and width_elem.text
            else 1
        )
        return bit_offset, bit_width

    range_elem = field_elem.find("bitRange")
    if range_elem is not None and range_elem.text:
        match = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", range_elem.text)
        if match:
            msb = int(match.group(1))
            lsb = int(match.group(2))
            return lsb, (msb - lsb + 1)

    lsb_elem = field_elem.find("lsb")
    msb_elem = field_elem.find("msb")
    if lsb_elem is not None and lsb_elem.text and msb_elem is not None and msb_elem.text:
        lsb = _parse_int(lsb_elem.text)
        msb = _parse_int(msb_elem.text)
        return lsb, (msb - lsb + 1)

    return 0, 1


def _parse_field(field_elem: ET.Element) -> SvdField:
    """Parse a single <field> XML element.

    Parameters
    ----------
    field_elem : ET.Element
        The XML element representing a register field.

    Returns
    -------
    SvdField
        Parsed immutable field model.
    """
    name = (field_elem.findtext("name") or "").strip()
    description = (field_elem.findtext("description") or "").strip()
    access = field_elem.findtext("access")
    reset_text = field_elem.findtext("resetValue")
    reset_val = _parse_int(reset_text) if reset_text is not None else None
    bit_offset, bit_width = _parse_bit_range(field_elem)

    return SvdField(
        name=name,
        description=description,
        bit_offset=bit_offset,
        bit_width=bit_width,
        access=access.strip() if access else None,
        reset_value=reset_val,
    )


def _parse_register(
    reg_elem: ET.Element, default_size: int, default_reset: int, default_mask: int
) -> SvdRegister:
    """Parse a single <register> XML element.

    Parameters
    ----------
    reg_elem : ET.Element
        The XML element representing a register.
    default_size : int
        Default register size in bits.
    default_reset : int
        Default reset value.
    default_mask : int
        Default reset mask.

    Returns
    -------
    SvdRegister
        Parsed immutable register model.
    """
    name = (reg_elem.findtext("name") or "").strip()
    display_name = reg_elem.findtext("displayName")
    description = (reg_elem.findtext("description") or "").strip()
    offset = _parse_int(reg_elem.findtext("addressOffset"))

    size_text = reg_elem.findtext("size")
    size = _parse_int(size_text, default=default_size) if size_text else default_size

    access = reg_elem.findtext("access")

    reset_val_text = reg_elem.findtext("resetValue")
    reset_value = (
        _parse_int(reset_val_text, default=default_reset) if reset_val_text else default_reset
    )

    reset_mask_text = reg_elem.findtext("resetMask")
    reset_mask = (
        _parse_int(reset_mask_text, default=default_mask) if reset_mask_text else default_mask
    )

    fields_elem = reg_elem.find("fields")
    fields: list[SvdField] = []
    if fields_elem is not None:
        for f_elem in fields_elem.findall("field"):
            fields.append(_parse_field(f_elem))

    return SvdRegister(
        name=name,
        display_name=display_name.strip() if display_name else None,
        description=description,
        address_offset=offset,
        size=size,
        access=access.strip() if access else None,
        reset_value=reset_value,
        reset_mask=reset_mask,
        fields=tuple(fields),
    )


def parse_svd_xml(xml_content: str | bytes) -> SvdDevice:
    """Parse SVD XML content into a structured SvdDevice object hierarchy.

    Parameters
    ----------
    xml_content : str | bytes
        Raw SVD XML string or bytes.

    Returns
    -------
    SvdDevice
        Decoded device and peripheral mapping.

    Raises
    ------
    SvdParseError
        If XML syntax is invalid or required fields are missing.
    """
    try:
        if isinstance(xml_content, str):
            root = ET.fromstring(xml_content)
        else:
            root = ET.fromstring(xml_content.decode("utf-8", errors="replace"))
    except ET.ParseError as error:
        raise SvdParseError(f"XML parse error: {error}") from error

    dev_name = (root.findtext("name") or "").strip()
    version = root.findtext("version")
    description = (root.findtext("description") or "").strip()
    vendor = root.findtext("vendor")
    address_unit_bits = _parse_int(root.findtext("addressUnitBits"), default=8)
    width = _parse_int(root.findtext("width"), default=32)
    dev_size = _parse_int(root.findtext("size"), default=32)
    dev_reset_val = _parse_int(root.findtext("resetValue"), default=0)
    dev_reset_mask = _parse_int(root.findtext("resetMask"), default=0xFFFFFFFF)

    periphs_elem = root.find("peripherals")
    if periphs_elem is None:
        raise SvdParseError("SVD XML document is missing <peripherals> root element.")

    raw_peripherals: dict[str, tuple[ET.Element, int, str, str | None, str | None]] = {}
    for p_elem in periphs_elem.findall("peripheral"):
        p_name = (p_elem.findtext("name") or "").strip()
        p_desc = (p_elem.findtext("description") or "").strip()
        p_group = p_elem.findtext("groupName")
        p_base = _parse_int(p_elem.findtext("baseAddress"))
        p_derived = p_elem.attrib.get("derivedFrom") or p_elem.findtext("derivedFrom")
        raw_peripherals[p_name] = (
            p_elem,
            p_base,
            p_desc,
            p_group.strip() if p_group else None,
            p_derived.strip() if p_derived else None,
        )

    peripherals_list: list[SvdPeripheral] = []
    registers_cache: dict[str, tuple[SvdRegister, ...]] = {}

    def get_peripheral_registers(name: str) -> tuple[SvdRegister, ...]:
        if name in registers_cache:
            return registers_cache[name]

        if name not in raw_peripherals:
            return ()

        p_elem, _, _, _, p_derived = raw_peripherals[name]
        regs_elem = p_elem.find("registers")

        regs: list[SvdRegister] = []
        if regs_elem is not None:
            for r_elem in regs_elem.findall("register"):
                regs.append(_parse_register(r_elem, dev_size, dev_reset_val, dev_reset_mask))

        if p_derived and p_derived in raw_peripherals:
            parent_regs = get_peripheral_registers(p_derived)
            if not regs:
                regs = list(parent_regs)
            else:
                existing_names = {r.name.upper() for r in regs}
                for parent_reg in parent_regs:
                    if parent_reg.name.upper() not in existing_names:
                        regs.append(parent_reg)

        result = tuple(regs)
        registers_cache[name] = result
        return result

    for p_name, (_, p_base, p_desc, p_group, p_derived) in raw_peripherals.items():
        regs = get_peripheral_registers(p_name)
        peripherals_list.append(
            SvdPeripheral(
                name=p_name,
                description=p_desc,
                group_name=p_group,
                base_address=p_base,
                derived_from=p_derived,
                registers=regs,
            )
        )

    return SvdDevice(
        name=dev_name,
        version=version.strip() if version else None,
        description=description,
        vendor=vendor.strip() if vendor else None,
        address_unit_bits=address_unit_bits,
        width=width,
        size=dev_size,
        reset_value=dev_reset_val,
        reset_mask=dev_reset_mask,
        peripherals=tuple(peripherals_list),
    )


def parse_svd_file(file_path: Path | str) -> SvdDevice:
    """Parse an SVD XML file from the local file system.

    Parameters
    ----------
    file_path : Path | str
        Path to the .svd file.

    Returns
    -------
    SvdDevice
        Parsed device object.

    Raises
    ------
    SvdParseError
        If file cannot be read or XML parsing fails.
    """
    path = Path(file_path)
    if not path.is_file():
        raise SvdParseError(f"SVD file not found: {path}")

    try:
        content = path.read_bytes()
    except OSError as error:
        raise SvdParseError(f"Failed to read SVD file {path}: {error}") from error

    return parse_svd_xml(content)


def load_svd_dict(file_path: Path | str) -> dict[str, Any]:
    """Parse an SVD file and return it as a complete dictionary hierarchy.

    Parameters
    ----------
    file_path : Path | str
        Path to the .svd file.

    Returns
    -------
    dict[str, Any]
        Dictionary representation of all peripherals, registers, and fields.
    """
    device = parse_svd_file(file_path)
    return device.to_dict()


def get_svd_by_name(
    vendor: str,
    svd_name: str,
    cache_dir: Path | None = None,
    force_download: bool = False,
    timeout: float = 15.0,
) -> SvdDevice:
    """Fetch an explicit SVD file by vendor and filename, and parse it.

    This primitive allows callers (such as user-facing GDB commands) to explicitly
    override or specify an SVD when auto-detection is not available or desired.

    Parameters
    ----------
    vendor : str
        Vendor name or folder (e.g. STMicro, Nordic, NXP).
    svd_name : str
        SVD file name or base name (e.g. STM32F401.svd or nrf52840).
    cache_dir : Path | None
        Optional custom cache directory.
    force_download : bool
        Whether to bypass cache and re-download.
    timeout : float
        HTTP download timeout in seconds.

    Returns
    -------
    SvdDevice
        The parsed SVD device definition.

    Raises
    ------
    SvdError
        If fetching or parsing fails.
    """
    vendor_dir = resolve_svd_vendor(vendor)
    svd_path = fetch_svd_file(
        vendor_dir=vendor_dir,
        filename=svd_name,
        cache_dir=cache_dir,
        force_download=force_download,
        timeout=timeout,
    )
    return parse_svd_file(svd_path)


def load_svd_dict_by_name(
    vendor: str,
    svd_name: str,
    cache_dir: Path | None = None,
    force_download: bool = False,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch an explicit SVD by vendor and name, returning its dictionary mapping.

    Parameters
    ----------
    vendor : str
        Vendor name or folder.
    svd_name : str
        SVD file name or base name.
    cache_dir : Path | None
        Optional custom cache directory.
    force_download : bool
        Whether to bypass cache and re-download.
    timeout : float
        HTTP download timeout in seconds.

    Returns
    -------
    dict[str, Any]
        Dictionary representation of the SVD device hierarchy.
    """
    device = get_svd_by_name(
        vendor=vendor,
        svd_name=svd_name,
        cache_dir=cache_dir,
        force_download=force_download,
        timeout=timeout,
    )
    return device.to_dict()


def get_svd_for_target(
    reader: TargetMemory | None = None,
    cpuid: CPUID | None = None,
    cache_dir: Path | None = None,
    force_download: bool = False,
) -> SvdDevice:
    """Detect the current target SoC, fetch its SVD file, and parse it.

    Parameters
    ----------
    reader : TargetMemory | None
        Typed reader for target memory. When None, TargetMemoryReader is used.
    cpuid : CPUID | None
        Optional pre-decoded CPUID. When None, it is read from target memory.
    cache_dir : Path | None
        Optional custom cache directory.
    force_download : bool
        Whether to bypass local cache and download fresh SVD.

    Returns
    -------
    SvdDevice
        The parsed SVD device definition for the active target.

    Raises
    ------
    SvdError
        If detection, fetching, or parsing fails.
    """
    if reader is None:
        reader = TargetMemoryReader()

    if cpuid is None:
        try:
            raw_cpuid = reader.read_uint32(CPUID_ADDRESS)
        except TargetReadError as error:
            raise SvdError(f"Cannot read target CPUID register: {error}") from error
        cpuid = decode_cpuid(raw_cpuid)

    report = DEFAULT_PROVIDER_REGISTRY.inspect(reader, cpuid)
    svd_path, _ = resolve_and_fetch_svd(report, cache_dir=cache_dir, force_download=force_download)
    return parse_svd_file(svd_path)


def load_svd_dict_for_target(
    reader: TargetMemory | None = None,
    cpuid: CPUID | None = None,
    cache_dir: Path | None = None,
    force_download: bool = False,
) -> dict[str, Any]:
    """Detect current target SoC, fetch its SVD, and return the complete dictionary.

    Parameters
    ----------
    reader : TargetMemory | None
        Typed reader for target memory. When None, TargetMemoryReader is used.
    cpuid : CPUID | None
        Optional pre-decoded CPUID.
    cache_dir : Path | None
        Optional custom cache directory.
    force_download : bool
        Whether to bypass local cache and download fresh SVD.

    Returns
    -------
    dict[str, Any]
        Dictionary representation of the target SVD.
    """
    device = get_svd_for_target(
        reader=reader,
        cpuid=cpuid,
        cache_dir=cache_dir,
        force_download=force_download,
    )
    return device.to_dict()
