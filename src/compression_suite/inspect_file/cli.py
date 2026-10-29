"""CLI command for inspect."""

from typing import Any

import typer

from compression_suite.inspect_file.main import inspect_file
from compression_suite.utils.contract import run_command


def _format_value(value: Any) -> str:
    """A tag or field value on one line: parts as `name=value`, lists comma-separated."""
    if isinstance(value, dict):
        return ", ".join(f"{part}={_format_value(part_value)}" for part, part_value in value.items())
    if isinstance(value, list):
        return ", ".join(_format_value(item) for item in value)
    return str(value)


def print_report(result: dict[str, Any]) -> None:
    """Write the description of a file on stdout, for a human."""
    motion_photo = result["motion_photo"]
    lines = [
        f"File: {result['path']}",
        f"Format: {result['format']} ({result['mime_type']})",
        f"Size: {result['size_bytes']:,} bytes",
        f"Modified: {result['mtime']}",
        f"Compressible: {'yes' if result['compressible'] else 'no'}",
        f"Motion photo: {'yes (' + ', '.join(motion_photo['signals']) + ')' if motion_photo['detected'] else 'no'}",
        "",
        "Important fields:",
    ]
    for field in result["important"]:
        value = "-" if field["value"] is None else _format_value(field["value"])
        source = f" ({field['source']})" if field["value"] is not None else ""
        lines.append(f"  {field['key']}: {value}{source}")
    lines += ["", "Metadata:"]
    lines += [f"  {tag}: {_format_value(value)}" for tag, value in result["metadata"].items()]
    typer.echo("\n".join(lines))


def inspect(
    input_file: str = typer.Argument(..., help="Path to the file to describe (any format)"),
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout"),
) -> None:
    """
    Describe any file: real format, size, dates, important fields, metadata and motion photo signals.

    Never contacts Tinify and needs no API key. An unknown format is not an error.
    """
    def body() -> dict[str, Any]:
        """Describe the file and return the `result` object of the JSON contract."""
        result = inspect_file(input_file)
        if not json_mode:
            print_report(result)
        return result

    run_command("inspect", json_mode, body)
