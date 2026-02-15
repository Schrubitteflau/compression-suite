"""Shared utilities for checking external tool dependencies and their versions."""

import re
import subprocess


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Parse a version string like '1.4.6' or '11.88' into a tuple of ints."""
    return tuple(int(x) for x in version_str.split("."))


def check_jpegoptim(
    min_version: tuple[int, ...] = (1, 4, 0),
    max_version_exclusive: tuple[int, ...] = (2,),
) -> str:
    """Verify jpegoptim is available and within the required version range.

    Parses version from output like 'jpegoptim v1.4.6  ...'.

    Returns:
        The detected version string.

    Raises:
        RuntimeError: If jpegoptim is not found or version is out of range.
    """
    try:
        result = subprocess.run(
            ["jpegoptim", "--version"], capture_output=True, text=True, timeout=5, check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("Required tool not found: jpegoptim")

    match = re.search(r"jpegoptim v(\d+\.\d+\.\d+)", result.stdout + result.stderr)
    if not match:
        raise RuntimeError(f"Could not parse jpegoptim version from output: {(result.stdout + result.stderr).strip()}")

    version_str = match.group(1)
    version = parse_version_tuple(version_str)

    if version < min_version or version >= max_version_exclusive:
        raise RuntimeError(
            f"jpegoptim version {version_str} is not supported. "
            f"Required: >= {'.'.join(map(str, min_version))} and < {'.'.join(map(str, max_version_exclusive))}"
        )

    return version_str


def check_exiftool(
    min_version: tuple[int, ...] = (11, 88),
    max_version_exclusive: tuple[int, ...] = (12,),
) -> str:
    """Verify exiftool is available and within the required version range.

    Parses version from `exiftool -ver` output like '11.88'.

    Returns:
        The detected version string.

    Raises:
        RuntimeError: If exiftool is not found or version is out of range.
    """
    try:
        result = subprocess.run(
            ["exiftool", "-ver"], capture_output=True, text=True, timeout=5, check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("Required tool not found: exiftool")

    version_str = result.stdout.strip()
    if not re.match(r"^\d+\.\d+$", version_str):
        raise RuntimeError(f"Could not parse exiftool version from output: {version_str!r}")

    version = parse_version_tuple(version_str)

    if version < min_version or version >= max_version_exclusive:
        raise RuntimeError(
            f"exiftool version {version_str} is not supported. "
            f"Required: >= {'.'.join(map(str, min_version))} and < {'.'.join(map(str, max_version_exclusive))}"
        )

    return version_str
