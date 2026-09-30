# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""WebSocket client and interactive dashboard for pyGdbServer."""

from .rpc import JsonRpcClient, RpcError

__all__ = ["JsonRpcClient", "RpcError"]
