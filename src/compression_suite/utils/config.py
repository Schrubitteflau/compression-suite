"""compression-suite's configuration file: `$XDG_CONFIG_HOME/compression-suite/config.toml` (`~/.config` by default).

It holds the Tinify key (`tinify_api_key`), so that no program calling compression-suite has to know it. `--api-key`
wins over the file; no environment variable is read. The key never appears in any output, message or log.
"""

import os
import stat
import sys
from pathlib import Path

from compression_suite.utils.cli import stderr_console
from compression_suite.utils.contract import CommandError, ErrorCode

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # ty: ignore[unresolved-import]  # only installed below 3.11

API_KEY_FIELD = "tinify_api_key"


def config_path() -> Path:
    """Where the configuration file is expected, whether it exists or not.

    A relative `XDG_CONFIG_HOME` is ignored, as the XDG Base Directory specification requires.
    """
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    return config_home / "compression-suite" / "config.toml"


def is_private(path: Path) -> bool:
    """Whether neither the group nor the others can read the file (which must exist)."""
    return not path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH)


def _is_blank(key: object) -> bool:
    """Whether a key is no usable string: not a string, empty or only spaces."""
    return not isinstance(key, str) or not key.strip()


def _api_key_missing(path: Path, reason: str) -> CommandError:
    """The `api_key_missing` error, naming the reason and the expected file."""
    return CommandError(
        ErrorCode.API_KEY_MISSING,
        f"No Tinify API key: {reason}. Pass --api-key, or write `{API_KEY_FIELD} = \"...\"` in {path} (chmod 600).",
        {"config_path": str(path)},
    )


def _read_config(path: Path) -> bytes:
    """The raw configuration file; raises `api_key_missing` when it is absent or unreadable."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise _api_key_missing(path, "the configuration file does not exist") from None
    except OSError as e:
        raise _api_key_missing(path, f"the configuration file can't be read ({e.strerror})") from None


def _parse_api_key(path: Path, content: bytes) -> str:
    """The key of the configuration file's content; raises `api_key_missing` when it is not valid TOML or has no
    usable key. The TOML error is not quoted: it could show a piece of the key.
    """
    try:
        config = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise _api_key_missing(path, "the configuration file is not valid TOML") from None
    key = config.get(API_KEY_FIELD)
    if _is_blank(key):
        raise _api_key_missing(path, f"the configuration file has no non-empty `{API_KEY_FIELD}` string")
    return key


def read_api_key(path: Path) -> str:
    """The key of the configuration file, warning on stderr if others can read it.

    Raises `api_key_missing` when the file is absent, unreadable, not valid TOML or without a usable key.
    """
    content = _read_config(path)
    if not is_private(path):
        stderr_console.print(
            f"[bold yellow]Warning:[/bold yellow] {path} is readable by other users, "
            f"who can see your Tinify key. Run: chmod 600 {path}",
            highlight=False,
            soft_wrap=True,
        )
    return _parse_api_key(path, content)


def has_api_key(path: Path) -> bool:
    """Whether the configuration file holds a usable key, silently: no warning, no error, never the key."""
    try:
        _parse_api_key(path, _read_config(path))
    except CommandError:
        return False
    return True


def resolve_api_key(api_key: str | None) -> str:
    """The `--api-key` value if given, else the configuration file's key.

    An empty `--api-key` (`--api-key="$KEY"` with `KEY` unset) is a missing key, not a fallback on the file: the
    caller meant another key than the file's.
    """
    if api_key is None:
        return read_api_key(config_path())
    if _is_blank(api_key):
        raise _api_key_missing(config_path(), "--api-key is empty")
    return api_key
