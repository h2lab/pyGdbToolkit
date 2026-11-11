# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Shared asynchronous HTTP and SSE plumbing for optional HTTP-based LLM adapters.

httpx stays an optional dependency: it is imported lazily by
:func:`open_client` only, and every transport failure is translated into a
sanitized :class:`ProviderError` so vendor exception text, prompts and
response bodies are never forwarded.
"""

import asyncio
from collections.abc import Awaitable
import math
from typing import TYPE_CHECKING, Any

from .contracts import JsonValue
from .provider import ProviderCancelledError, ProviderError

if TYPE_CHECKING:
    from httpx import AsyncBaseTransport, AsyncClient
else:
    # Runtime placeholders keep httpx an optional dependency: annotations are
    # evaluated eagerly before Python 3.14, and httpx is only imported lazily.
    AsyncBaseTransport = AsyncClient = Any

__all__ = [
    "SSE_DONE",
    "AsyncBaseTransport",
    "AsyncClient",
    "finite_json",
    "open_client",
    "sse_data",
    "step",
]

SSE_DONE = "[DONE]"


def open_client(
    base_url: str,
    headers: dict[str, str],
    timeout: float,
    transport: AsyncBaseTransport | None,
    guidance: str,
) -> AsyncClient:
    """Import httpx lazily and create an isolated asynchronous client.

    Parameters
    ----------
    base_url : str
        Absolute API base URL; relative request paths are joined below it.
    headers : dict[str, str]
        Headers sent with every request, such as an authorization header.
    timeout : float
        Finite bound applied to connection and read operations.
    transport : httpx.AsyncBaseTransport, optional
        Replacement transport for offline tests; production uses the default.
    guidance : str
        Sanitized installation guidance raised when httpx is missing.

    Returns
    -------
    httpx.AsyncClient
        A client that never follows redirects and ignores proxy environment
        variables: audit data must reach the configured endpoint directly,
        never an ambient intermediary.

    Raises
    ------
    ProviderError
        If the optional httpx dependency is not installed.
    """
    try:
        import httpx
    except ImportError:
        raise ProviderError(guidance) from None
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        transport=transport,
        follow_redirects=False,
        trust_env=False,
    )


async def step[T](  # noqa: D103 -- pydocstyle cannot parse PEP 695 generics
    operation: Awaitable[T],
    stage: str,
    *,
    timeout: float,
    cancel_event: asyncio.Event,
    label: str,
) -> T:
    """Await one transport step, racing cancellation and a finite timeout.

    Parameters
    ----------
    operation : Awaitable[T]
        The transport operation to await, such as a request or a stream read.
    stage : str
        Short stage name included in sanitized error messages.
    timeout : float
        Finite bound on the step.
    cancel_event : asyncio.Event
        Turn-cancellation signal; setting it interrupts the pending step.
    label : str
        Adapter label prefixed to sanitized error messages.

    Returns
    -------
    T
        The operation result.

    Raises
    ------
    ProviderCancelledError
        If the cancellation event fired before the operation completed.
    ProviderError
        On timeout or any transport failure, with sanitized guidance;
        ``StopAsyncIteration`` propagates unchanged to signal stream ends.
    """
    waiter = asyncio.ensure_future(operation)
    canceller = asyncio.ensure_future(cancel_event.wait())
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait((waiter, canceller), return_when=asyncio.FIRST_COMPLETED)
    except TimeoutError:
        raise ProviderError(f"{label} {stage} timed out; check the server.") from None
    finally:
        waiter.cancel()
        canceller.cancel()
        await asyncio.gather(waiter, canceller, return_exceptions=True)
    if not waiter.cancelled():
        try:
            return waiter.result()
        except (StopAsyncIteration, ProviderError):
            raise
        except Exception:
            # Vendor errors can echo prompts or the response; never forward them.
            raise ProviderError(
                f"{label} {stage} failed; check the endpoint, network and key."
            ) from None
    raise ProviderCancelledError("the turn was cancelled")


def sse_data(line: str) -> str | None:
    """Return the payload of one server-sent-events data line.

    Parameters
    ----------
    line : str
        One line of an SSE stream, without its line terminator.

    Returns
    -------
    str or None
        The stripped ``data:`` payload, or None for every other line,
        including comments, other fields and event separators.
    """
    if not line.startswith("data:"):
        return None
    return line[len("data:") :].strip()


def finite_json(value: object) -> JsonValue:
    """Validate an endpoint payload as JSON containing only finite numbers.

    Parameters
    ----------
    value : object
        A decoded JSON payload received from the endpoint.

    Returns
    -------
    JsonValue
        The same structure, typed as validated JSON.

    Raises
    ------
    ProviderError
        If the payload contains non-finite numbers or non-JSON values.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [finite_json(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: finite_json(item) for key, item in value.items()}
    raise ProviderError("the endpoint returned values that are not finite JSON")
