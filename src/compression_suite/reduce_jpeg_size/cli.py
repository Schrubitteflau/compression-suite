"""CLI command for reduce-jpeg-size."""

from typing import Optional

import typer

from compression_suite.reduce_jpeg_size.main import main
from compression_suite.utils.cli import cli_error_handler


@cli_error_handler
def reduce_jpeg_size(
    input_file: Optional[str] = typer.Argument(None, help="Path to input JPEG file (omit to read from stdin)"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Path to output file (omit to write to stdout)"),
    max_size: int = typer.Option(4999, "--max-size", help="Target maximum size in KB (default: 4999)"),
    max_iterations: int = typer.Option(10, "--max-iterations", help="Maximum number of compression iterations (default: 10)"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite output file if it exists"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
) -> None:
    """
    Reduce JPEG file size using jpegoptim.

    Iteratively compresses a JPEG file until it fits within the target size.
    Reads from a file or stdin, writes to a file or stdout.
    """
    main(
        input_file=input_file,
        output=output,
        max_size=max_size,
        max_iterations=max_iterations,
        overwrite=overwrite,
        verbose=verbose,
    )
