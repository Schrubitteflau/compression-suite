"""compression-suite's configuration file: `$XDG_CONFIG_HOME/compression-suite/config.toml` (`~/.config` by default).

It holds the Tinify key (`tinify_api_key`), so that no caller, WelcomeApp included, has to know it. `--api-key`
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
    """Where the configuration file is expected, whether it exists or not."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "compression-suite" / "config.toml"


def permissions_ok(path: Path) -> bool:
    """Whether neither the group nor the others can read the file."""
    return not path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH)


def _api_key_missing(path: Path, reason: str) -> CommandError:
    return CommandError(
        ErrorCode.API_KEY_MISSING,
        f"No Tinify API key: {reason}. Pass --api-key, or write `{API_KEY_FIELD} = \"...\"` in {path} (chmod 600).",
        {"config_path": str(path)},
    )


def read_api_key(path: Path) -> str:
    """The key of the configuration file, warning on stderr if others can read it.

    Raises `api_key_missing` when the file is absent, unreadable, not valid TOML or without a non-empty key. The
    TOML error is not quoted: it could show a piece of the key.
    """
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        raise _api_key_missing(path, "the configuration file does not exist") from None
    except OSError as e:
        raise _api_key_missing(path, f"the configuration file can't be read ({e.strerror})") from None
    if not permissions_ok(path):
        stderr_console.print(
            f"[bold yellow]Warning:[/bold yellow] {path} is readable by other users, "
            f"who can see your Tinify key. Run: chmod 600 {path}",
            highlight=False,
            soft_wrap=True,
        )
    try:
        config = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise _api_key_missing(path, "the configuration file is not valid TOML") from None
    key = config.get(API_KEY_FIELD)
    if not isinstance(key, str) or not key:
        raise _api_key_missing(path, f"the configuration file has no `{API_KEY_FIELD}` string")
    return key


def resolve_api_key(api_key: str | None) -> str:
    """The `--api-key` value if given, else the configuration file's key."""
    return api_key if api_key is not None else read_api_key(config_path())
