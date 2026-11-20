# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Workspace filename validation and downloads shared by dashboard and agent tools."""

import base64
import binascii
from pathlib import Path
from tempfile import NamedTemporaryFile

from .llm.context import RpcRequester

WORKSPACE_FILENAME_MAX_LENGTH = 128
WORKSPACE_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
AGENT_DOWNLOAD_MAX_BYTES = 5 * 1024 * 1024


def validate_workspace_filename(filename: str) -> str:
    """Validate a server workspace file name before it is sent.

    Parameters
    ----------
    filename : str
        Candidate server-side file name.

    Returns
    -------
    str
        The validated file name.

    Raises
    ------
    ValueError
        If the name is empty, contains directory separators or components
        (including ``.`` and ``..``), or exceeds 128 characters.
    """
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("server file must be a file name without directory components")
    if len(filename) > WORKSPACE_FILENAME_MAX_LENGTH:
        raise ValueError(
            f"server file name must not exceed {WORKSPACE_FILENAME_MAX_LENGTH} characters"
        )
    return filename


async def download_workspace_file(
    client: RpcRequester,
    filename: str,
    local_directory: Path,
    *,
    timeout: float = 300,
    max_bytes: int | None = None,
) -> tuple[Path, int]:
    """Download validated chunks and publish the local file only on success."""
    validate_workspace_filename(filename)
    local_directory = local_directory.resolve()
    if not local_directory.is_dir():
        raise NotADirectoryError(f"Not a directory: {local_directory}")
    destination = local_directory / filename
    temporary_path: Path | None = None
    total_size: int | None = None
    offset = 0
    try:
        with NamedTemporaryFile(dir=local_directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            while total_size is None or offset < total_size:
                response = await client.request(
                    "workspace.download",
                    {
                        "filename": filename,
                        "offset": offset,
                        "chunk_size": WORKSPACE_DOWNLOAD_CHUNK_SIZE,
                    },
                    timeout=timeout,
                )
                total_size, content = _decode_download_chunk(response, filename, offset, total_size)
                if max_bytes is not None and total_size > max_bytes:
                    raise ValueError(f"workspace download exceeds the {max_bytes}-byte agent limit")
                temporary.write(content)
                offset += len(content)
        temporary_path.replace(destination)
        return destination, total_size
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _decode_download_chunk(
    response: object, filename: str, offset: int, expected_size: int | None
) -> tuple[int, bytes]:
    if not isinstance(response, dict):
        raise ValueError("invalid download response")
    response_filename = response.get("filename")
    response_offset = response.get("offset")
    size = response.get("size")
    encoded_content = response.get("content")
    if response_filename != filename:
        raise ValueError("download response filename does not match the request")
    if (
        isinstance(response_offset, bool)
        or not isinstance(response_offset, int)
        or response_offset != offset
    ):
        raise ValueError("download response offset does not match the request")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("download response has an invalid file size")
    if expected_size is not None and size != expected_size:
        raise ValueError("server file changed while downloading")
    if not isinstance(encoded_content, str):
        raise ValueError("download response content must be base64")
    expected_chunk_size = min(WORKSPACE_DOWNLOAD_CHUNK_SIZE, size - offset)
    if expected_chunk_size < 0 or len(encoded_content) != 4 * ((expected_chunk_size + 2) // 3):
        raise ValueError("download response has an invalid chunk size")
    try:
        content = base64.b64decode(encoded_content, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("download response content is not valid base64") from error
    if len(content) != expected_chunk_size:
        raise ValueError("download response has an invalid chunk size")
    return size, content
