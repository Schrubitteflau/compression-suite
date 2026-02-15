"""Shared CLI error handling."""

import functools
from collections.abc import Callable

import typer
from rich.console import Console

EXIT_USER_ERROR = 50
EXIT_INTERRUPT = 130

stderr_console = Console(stderr=True)


def cli_error_handler(func: Callable) -> Callable:
    """Wrap a CLI command to catch common exceptions with consistent exit codes.

    Module-specific exceptions should be caught inside the wrapped function
    before they bubble up to this handler.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (typer.Exit, SystemExit):
            raise
        except KeyboardInterrupt:
            stderr_console.print("\n[bold yellow]Interrupted by user[/bold yellow]")
            raise typer.Exit(code=EXIT_INTERRUPT)
        except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as e:
            stderr_console.print(f"[bold red]Error:[/bold red] {e}")
            raise typer.Exit(code=EXIT_USER_ERROR)
        except Exception as e:
            stderr_console.print(f"[bold red]Unexpected error:[/bold red] {e}")
            raise typer.Exit(code=EXIT_USER_ERROR)

    return wrapper
