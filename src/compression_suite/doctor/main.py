"""Core logic for doctor: what compression-suite really runs, and whether a Tinify key is configured.

The report never holds the key itself, only where it is looked for and whether the file is private.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from compression_suite.utils.config import config_path, has_api_key, is_private
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.pinned_tools import (
    INSTALL_COMMAND,
    PinnedTool,
    expected_executable,
    installed_version,
    load_manifest,
)

# Commands relying on each external program; a pinned tool missing here is reported with no command.
USED_BY: dict[str, list[str]] = {
    "exiftool": ["compress-image", "inspect"],
    "ffmpeg": ["extract-unique-frames", "reassemble-video"],
    "jpegoptim": ["reduce-jpeg-size"],
}


@dataclass(frozen=True)
class SystemDependency:
    """A program still taken from the system's `PATH`, and how to read its version."""

    name: str
    version_args: tuple[str, ...]
    version_pattern: str
    used_by: list[str]


SYSTEM_DEPENDENCIES = [
    SystemDependency("perl", ("-e", "print $^V"), r"^v?(\S+)", USED_BY["exiftool"]),  # runs the pinned exiftool
    SystemDependency("ffmpeg", ("-version",), r"ffmpeg version (\S+)", USED_BY["ffmpeg"]),
    SystemDependency("jpegoptim", ("--version",), r"jpegoptim v(\S+)", USED_BY["jpegoptim"]),
]


def pinned_tool_report(tool: PinnedTool) -> dict[str, Any]:
    """The state of one pinned tool: `ok`, `missing` (absent or can't run) or `version_mismatch`."""
    executable = expected_executable(tool)
    found_version = installed_version(tool, executable) if executable.is_file() else None
    if found_version is None:
        status = "missing"
    elif found_version != tool.version:
        status = "version_mismatch"
    else:
        status = "ok"
    return {
        "name": tool.name,
        "expected_version": tool.version,
        "found_version": found_version,
        "path": str(executable),
        "status": status,
        "used_by": USED_BY.get(tool.name, []),
    }


def system_version(dependency: SystemDependency, executable: str) -> str | None:
    """Run a system program to read its version, or None if it can't be run or its output isn't understood."""
    try:
        result = subprocess.run(
            [executable, *dependency.version_args], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(dependency.version_pattern, result.stdout + result.stderr, re.MULTILINE)
    return match.group(1) if match else None


def system_dependency_report(dependency: SystemDependency) -> dict[str, Any]:
    """The state of one system dependency, looked up in `PATH`: `ok` or `missing`."""
    executable = shutil.which(dependency.name)
    return {
        "name": dependency.name,
        "found_version": system_version(dependency, executable) if executable else None,
        "path": executable,
        "status": "ok" if executable else "missing",
        "used_by": dependency.used_by,
    }


def tinify_key_report() -> dict[str, Any]:
    """Whether a Tinify key is configured and where it is looked for, never the key itself.

    `permissions_ok` is None when the configuration file does not exist.
    """
    path = config_path()
    configured = has_api_key(path)
    try:
        permissions_ok = is_private(path)
    except FileNotFoundError:
        permissions_ok = None
    return {
        "configured": configured,
        "source": "config_file" if configured else "none",
        "config_path": str(path),
        "permissions_ok": permissions_ok,
    }


def doctor_report() -> dict[str, Any]:
    """The whole report: pinned tools, system dependencies and Tinify key."""
    return {
        "pinned_tools": [pinned_tool_report(tool) for tool in load_manifest().values()],
        "system_dependencies": [system_dependency_report(dependency) for dependency in SYSTEM_DEPENDENCIES],
        "tinify_api_key": tinify_key_report(),
    }


def check_pinned_tools(report: dict[str, Any]) -> None:
    """Raise `tool_missing` if a pinned tool is not `ok`, carrying the whole report in its details.

    A missing system dependency or Tinify key is not a failure: only the pinned tools are required.
    """
    failing = [tool for tool in report["pinned_tools"] if tool["status"] != "ok"]
    if failing:
        names = ", ".join(f"{tool['name']} ({tool['status']})" for tool in failing)
        raise CommandError(
            ErrorCode.TOOL_MISSING,
            f"Pinned tools not usable: {names}. Install them with: {INSTALL_COMMAND}",
            {**report, "install_command": INSTALL_COMMAND},
        )
