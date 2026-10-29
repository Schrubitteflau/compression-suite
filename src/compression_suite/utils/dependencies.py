"""Shared utilities for checking system tool dependencies and their versions (pinned tools live in `pinned_tools`)."""

import re
import subprocess


def read_version(command: list[str], pattern: str) -> str | None:
    """Run a version command and return the first group of `pattern` in its stdout, or None if it can't be run or
    its output isn't understood.
    """
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(pattern, result.stdout.strip(), re.MULTILINE)
    return match.group(1) if match else None


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

