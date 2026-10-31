"""Shared CLI error handling."""

import functools
from collections.abc import Callable

import typer
from rich.console import Console
from rich.markup import escape

EXIT_USER_ERROR = 50
EXIT_INTERRUPT = 130

stderr_console = Console(stderr=True)


def cli_error_handler(func: Callable) -> Callable:
    """Wrap a CLI command to catch common exceptions with consistent exit codes.

    A `CommandError` exits with the exit code of its error code, as in the JSON contract.

    Module-specific exceptions should be caught inside the wrapped function
    before they bubble up to this handler.
    """

    # Imported here: the contract module imports this one for its console
    from compression_suite.utils.contract import CommandError

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (typer.Exit, SystemExit):
            raise
        except CommandError as e:  # a stable error (`tool_missing`...): its own exit code
            # One line, never wrapped: the message names a path and the install command
            stderr_console.print(f"[bold red]Error:[/bold red] {escape(e.message)}", soft_wrap=True)
            raise typer.Exit(code=e.code.exit_code) from None
        except KeyboardInterrupt:
            stderr_console.print("\n[bold yellow]Interrupted by user[/bold yellow]")
            raise typer.Exit(code=EXIT_INTERRUPT) from None
        except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(code=EXIT_USER_ERROR) from None
        except Exception as e:
            stderr_console.print(f"[bold red]Unexpected error:[/bold red] {e}")
            raise typer.Exit(code=EXIT_USER_ERROR) from None

    return wrapper
