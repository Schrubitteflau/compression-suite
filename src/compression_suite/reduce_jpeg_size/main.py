"""Core logic for reduce-size: iterative JPEG size reduction via jpegoptim."""

import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console

from compression_suite.utils.dependencies import check_jpegoptim


def reduce_size(
    input_file: str | None,
    output: str | None,
    max_size: int,
    max_iterations: int,
    overwrite: bool,
    verbose: bool,
) -> None:
    """Reduce JPEG file size using jpegoptim with iterative passes.

    Args:
        input_file: Path to input JPEG, or None for stdin.
        output: Path to output file, or None for stdout.
        max_size: Target max size in KB.
        max_iterations: Maximum number of compression iterations.
        overwrite: Allow overwriting existing output file.
        verbose: Enable verbose reporting.
    """
    console = Console(stderr=True)

    version = check_jpegoptim()
    if verbose:
        console.print(f"[dim]jpegoptim version: {version}[/dim]")

    # Read input
    if input_file is not None:
        input_path = Path(input_file)
        if not input_path.exists():
            raise FileNotFoundError(f"Input file does not exist: {input_file}")
        if not input_path.is_file():
            raise ValueError(f"Input path is not a file: {input_file}")
        input_data = input_path.read_bytes()
    else:
        input_data = sys.stdin.buffer.read()

    input_size = len(input_data)

    # Check output path before processing
    if output is not None:
        output_path = Path(output)
        if output_path.exists() and not overwrite:
            raise FileExistsError(
                f"Output file already exists: {output}. Use --overwrite to replace."
            )

    max_size_bytes = max_size * 1024

    with tempfile.TemporaryDirectory() as tmpdir:
        working_file = Path(tmpdir) / "working.jpg"
        working_file.write_bytes(input_data)

        # First pass: target size directly
        subprocess.run(
            ["jpegoptim", f"--size={max_size}", str(working_file)],
            capture_output=True, timeout=60, check=False,
        )

        iterations = 1
        current_size = working_file.stat().st_size

        # Iterative passes with escalating reduction percentage
        # Start at 2%, escalate to 3%, 4%... until progress is made,
        # then reset to 1% for the next iteration.
        reduction_pct = 2
        while current_size > max_size_bytes and iterations < max_iterations:
            previous_size = current_size
            target_pct = 100 - reduction_pct
            subprocess.run(
                ["jpegoptim", f"--size={target_pct}%", str(working_file)],
                capture_output=True, timeout=60, check=False,
            )
            current_size = working_file.stat().st_size
            iterations += 1

            if current_size < previous_size:
                # Progress made — reset to gentle 1% for next iteration
                reduction_pct = 1
                if verbose:
                    console.print(f"[dim]Iteration {iterations}: {current_size:,} bytes (−{reduction_pct}% worked, resetting)[/dim]")
            else:
                # No progress — escalate reduction percentage
                reduction_pct += 1
                if verbose:
                    console.print(f"[dim]Iteration {iterations}: no progress at {target_pct}%, escalating to {100 - reduction_pct}%[/dim]")
                if reduction_pct > 50:
                    if verbose:
                        console.print("[dim]Escalation limit reached (50%), stopping.[/dim]")
                    break

        result_data = working_file.read_bytes()

    output_size = len(result_data)

    # Write output
    if output is not None:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(result_data)
    else:
        sys.stdout.buffer.write(result_data)

    # Report on stderr
    ratio = (1 - output_size / input_size) * 100 if input_size > 0 else 0
    console.print(f"[bold green]Reduced:[/bold green] {input_size:,} → {output_size:,} bytes ({ratio:.1f}% reduction)")
    console.print(f"[dim]Iterations: {iterations}[/dim]")


def main(
    input_file: str | None,
    output: str | None,
    max_size: int,
    max_iterations: int,
    overwrite: bool,
    verbose: bool,
) -> None:
    """Entry point called from cli.py."""
    reduce_size(input_file, output, max_size, max_iterations, overwrite, verbose)
