# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Arm device-provider registry and manufacturer implementations."""

from .base import DeviceProvider, ProviderRegistry
from .manufacturers.stm32 import Stm32Provider

DEFAULT_PROVIDER_REGISTRY = ProviderRegistry((Stm32Provider(),))

__all__ = [
    "DEFAULT_PROVIDER_REGISTRY",
    "DeviceProvider",
    "ProviderRegistry",
    "Stm32Provider",
]
