"""Shared machine-readable CLI contract: JSON envelope, stable error codes and exit codes.

Every command that supports ``--json`` writes a single document on stdout:

- success: ``{"schema_version": 1, "ok": true, "command": "<name>", "result": {...}}``
- failure: ``{"schema_version": 1, "ok": false, "command": "<name>",
  "error": {"code": "<code>", "message": "<text>", "details": {...}}}``

Exit codes are the same with or without ``--json``. Any incompatible change to the
document shape must bump ``SCHEMA_VERSION``.
"""

import json
from collections.abc import Callable
from enum import Enum
from typing import Any

import typer

from compression_suite.utils.cli import stderr_console

SCHEMA_VERSION = 1


class ErrorCode(str, Enum):
    """Stable textual error codes, each bound to a dedicated exit code."""

    TINIFY_UNAVAILABLE = "tinify_unavailable"
    API_KEY_INVALID = "api_key_invalid"
    QUOTA_EXCEEDED = "quota_exceeded"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FILE_TOO_LARGE = "file_too_large"
    INPUT_NOT_FOUND = "input_not_found"
    OUTPUT_EXISTS = "output_exists"
    METADATA_COPY_FAILED = "metadata_copy_failed"
    USAGE_ERROR = "usage_error"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self]


EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.TINIFY_UNAVAILABLE: 10,
    ErrorCode.API_KEY_INVALID: 11,
    ErrorCode.QUOTA_EXCEEDED: 12,
    ErrorCode.UNSUPPORTED_FORMAT: 21,
    ErrorCode.FILE_TOO_LARGE: 22,
    ErrorCode.INPUT_NOT_FOUND: 23,
    ErrorCode.OUTPUT_EXISTS: 24,
    ErrorCode.METADATA_COPY_FAILED: 25,
    ErrorCode.USAGE_ERROR: 2,
    ErrorCode.INTERRUPTED: 130,
    ErrorCode.INTERNAL_ERROR: 50,
}


class CommandError(Exception):
    """A failure with a stable error code, a human-readable message and structured details."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def success_envelope(command: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "ok": True, "command": command, "result": result}


def error_envelope(command: str, error: CommandError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": {"code": error.code.value, "message": error.message, "details": error.details},
    }


def run_command(command: str, json_mode: bool, func: Callable[[], dict[str, Any]]) -> None:
    """Run a command body and report its outcome according to the contract.

    In JSON mode, the envelope is the only thing written on stdout. Otherwise errors
    are printed on stderr. Either way, failures exit with the code bound to their error code.
    """
    try:
        result = func()
    except (typer.Exit, SystemExit):
        raise
    except CommandError as e:
        error = e
    except KeyboardInterrupt:
        error = CommandError(ErrorCode.INTERRUPTED, "Interrupted by user")
    except Exception as e:
        error = CommandError(ErrorCode.INTERNAL_ERROR, f"Unexpected error: {e}")
    else:
        if json_mode:
            typer.echo(json.dumps(success_envelope(command, result)))
        return

    if json_mode:
        typer.echo(json.dumps(error_envelope(command, error)))
    else:
        stderr_console.print(f"[bold red]Error:[/bold red] {error.message}", highlight=False)
    raise typer.Exit(code=error.code.exit_code)
