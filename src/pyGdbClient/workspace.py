# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Workspace filename validation shared by dashboard commands and agent tools."""

WORKSPACE_FILENAME_MAX_LENGTH = 128


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
