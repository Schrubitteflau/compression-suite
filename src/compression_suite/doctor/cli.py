"""CLI command for doctor."""

from typing import Any

import typer

from compression_suite.doctor.main import check_pinned_tools, doctor_report
from compression_suite.utils.contract import run_command


def _used_by(entry: dict[str, Any]) -> str:
    """The commands relying on a tool, for a human."""
    return f"used by {', '.join(entry['used_by'])}" if entry["used_by"] else "used by no command"


def _permissions(key_report: dict[str, Any]) -> str:
    """The permissions line of the Tinify key file."""
    if key_report["permissions_ok"] is None:
        return "- (no configuration file)"
    if key_report["permissions_ok"]:
        return "ok (600: open to you alone)"
    return f"open to other users. Run: chmod 600 {key_report['config_path']}"


def print_report(report: dict[str, Any]) -> None:
    """Write the doctor report on stdout, for a human. It never holds the key."""
    lines = ["Pinned tools:"]
    for tool in report["pinned_tools"]:
        found = tool["found_version"] or "-"
        lines.append(
            f"  {tool['name']}: {tool['status']} (expected {tool['expected_version']}, found {found}) "
            f"{tool['path']}, {_used_by(tool)}"
        )
    lines += ["", "System dependencies:"]
    for dependency in report["system_dependencies"]:
        found = f"{dependency['found_version'] or 'unknown version'} {dependency['path']}"
        state = found if dependency["status"] == "ok" else "missing"
        lines.append(f"  {dependency['name']}: {state}, {_used_by(dependency)}")
    key_report = report["tinify_api_key"]
    lines += [
        "",
        "Tinify API key:",
        f"  Configured: {'yes' if key_report['configured'] else 'no'} (source: {key_report['source']})",
        f"  Configuration file: {key_report['config_path']}",
        f"  Permissions: {_permissions(key_report)}",
    ]
    typer.echo("\n".join(lines))


def doctor(
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout"),
) -> None:
    """
    Show the external tools compression-suite really runs, and whether a Tinify key is configured.

    Fails with tool_missing (exit 30) if a pinned tool is not usable; a missing system dependency or key does not.
    The key is never shown.
    """

    def body() -> dict[str, Any]:
        """Build the report and return the `result` object of the JSON contract."""
        report = doctor_report()
        if not json_mode:
            print_report(report)
        check_pinned_tools(report)
        return report

    run_command("doctor", json_mode, body)
