"""Pinned tools: external programs downloaded into the project at the exact version of a versioned manifest.

`pinned-tools.json`, at the project root, gives for each tool its version, a stable download URL,
the SHA-256 of the archive and how to read the version back. `just install-tools` downloads,
verifies and extracts every tool into `.tools/<name>-<version>/`, ignored by git.

`resolve_tool` is the only way to get a pinned tool: it never looks at `PATH`, and it checks
that the installed tool reports the manifest's version.

The manifest and `.tools/` are found relative to this file, so compression-suite must run from
its checkout (`uv run`, editable install), not from a wheel installed elsewhere.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from compression_suite.utils.contract import CommandError, ErrorCode

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = PROJECT_ROOT / "pinned-tools.json"
DEFAULT_TOOLS_DIR = PROJECT_ROOT / ".tools"
TOOLS_DIR_ENV = "COMPRESSION_SUITE_TOOLS_DIR"
INSTALL_COMMAND = "just install-tools"


@dataclass(frozen=True)
class PinnedTool:
    """One entry of the manifest."""

    name: str
    version: str
    url: str
    sha256: str
    archive_root: str
    executable: str
    version_args: tuple[str, ...]
    version_pattern: str


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, PinnedTool]:
    """Read the manifest, keyed by tool name."""
    entries = json.loads(path.read_text())
    return {
        name: PinnedTool(
            name=name,
            version=entry["version"],
            url=entry["url"],
            sha256=entry["sha256"],
            archive_root=entry["archive_root"],
            executable=entry["executable"],
            version_args=tuple(entry["version_args"]),
            version_pattern=entry["version_pattern"],
        )
        for name, entry in entries.items()
    }


def tools_dir() -> Path:
    """Folder holding the installed pinned tools (overridable through the environment)."""
    override = os.environ.get(TOOLS_DIR_ENV)
    return Path(override) if override else DEFAULT_TOOLS_DIR


def install_dir(tool: PinnedTool, root: Path) -> Path:
    """Folder where a tool is installed, named after its version so that a bump never reuses an old install."""
    return root / f"{tool.name}-{tool.version}"


def expected_executable(tool: PinnedTool) -> Path:
    """Where the tool's executable is expected, whether it is installed or not."""
    return install_dir(tool, tools_dir()) / tool.executable


def installed_version(tool: PinnedTool, executable: Path) -> str | None:
    """Run the tool to read its version, or None if it can't be run or its output isn't understood."""
    try:
        result = subprocess.run(
            [str(executable), *tool.version_args], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(tool.version_pattern, result.stdout.strip(), re.MULTILINE)
    return match.group(1) if match else None


def is_installed(tool: PinnedTool, executable: Path) -> bool:
    """Whether the executable exists and reports the manifest's version."""
    return executable.is_file() and installed_version(tool, executable) == tool.version


def tool_missing_error(tool: PinnedTool, reason: str) -> CommandError:
    """Build the `tool_missing` error of a pinned tool."""
    return CommandError(
        ErrorCode.TOOL_MISSING,
        f"Pinned tool {tool.name} {tool.version} {reason}. Install it with: {INSTALL_COMMAND}",
        {"tool": tool.name, "expected_version": tool.version, "install_command": INSTALL_COMMAND},
    )


def resolve_tool(name: str) -> Path:
    """Give the path of a pinned tool, after checking it reports the manifest's version.

    Raises:
        CommandError: `tool_missing` if the tool is not installed, can't run, or has another version.
    """
    tool = load_manifest()[name]
    executable = expected_executable(tool)
    if not executable.is_file():
        raise tool_missing_error(tool, f"is not installed (expected at {executable})")
    version = installed_version(tool, executable)
    if version is None:
        raise tool_missing_error(tool, f"can't be run or its version can't be read ({executable})")
    if version != tool.version:
        raise tool_missing_error(tool, f"is expected, found version {version} ({executable})")
    return executable


def sha256_of(path: Path) -> str:
    """Hash a file by chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_tool(tool: PinnedTool, root: Path) -> Path:
    """Download, verify and extract one tool; do nothing if the right version is already installed.

    The archive is extracted in a temporary folder next to its destination, then renamed, so that
    an interrupted install never leaves a half-extracted tool behind.
    """
    destination = install_dir(tool, root)
    executable = destination / tool.executable
    if is_installed(tool, executable):
        print(f"{tool.name} {tool.version} already installed in {destination}")
        return executable

    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix=f".{tool.name}-") as tmp:
        archive = Path(tmp) / "archive.tar.gz"
        print(f"Downloading {tool.name} {tool.version} from {tool.url}")
        with urllib.request.urlopen(tool.url, timeout=60) as response, archive.open("wb") as f:
            shutil.copyfileobj(response, f)

        digest = sha256_of(archive)
        if digest != tool.sha256:
            raise RuntimeError(f"SHA-256 mismatch for {tool.url}: expected {tool.sha256}, got {digest}")

        extracted = Path(tmp) / "extracted"
        with tarfile.open(archive) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(extracted, filter="data")
            else:  # Python without extraction filters (< 3.10.12)
                tar.extractall(extracted)

        if destination.exists():
            shutil.rmtree(destination)
        (extracted / tool.archive_root).rename(destination)

    version = installed_version(tool, executable)
    if version != tool.version:
        raise RuntimeError(f"{tool.name} installed in {destination} reports version {version}, not {tool.version}")
    print(f"{tool.name} {tool.version} installed in {destination}")
    return executable


def install_all() -> None:
    """Install every tool of the manifest into the tools folder."""
    root = tools_dir()
    for tool in load_manifest().values():
        install_tool(tool, root)


if __name__ == "__main__":
    try:
        install_all()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
