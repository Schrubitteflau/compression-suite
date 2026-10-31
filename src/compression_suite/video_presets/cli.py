"""CLI command for video-presets."""

from typing import Any

import typer

from compression_suite.utils.contract import run_command
from compression_suite.utils.video_presets import DEFAULT_PRESET_ID, PRESETS


def print_presets(result: dict[str, Any]) -> None:
    """Write the presets on stdout, for a human."""
    lines = [f"Default preset: {result['default_preset']}", ""]
    for preset in result["presets"]:
        video = preset["video"]
        audio = preset["audio"]
        sound = f"{audio['codec']} {audio['bitrate_kbps']}k" if audio else "no audio"
        lines.append(
            f"{preset['id']}: {preset['label']} ({preset['container']}, {video['encoder']} {video['speed']}, "
            f"CRF {video['crf']} ({preset['crf_bounds']['min']} to {preset['crf_bounds']['max']}), {sound})"
        )
    typer.echo("\n".join(lines))


def video_presets(
    json_mode: bool = typer.Option(False, "--json", help="Write a single JSON document on stdout"),
) -> None:
    """
    List the named video presets, with their default CRF and the CRF bounds a command that encodes accepts.
    """

    def body() -> dict[str, Any]:
        """Return the `result` object of the JSON contract."""
        result = {"default_preset": DEFAULT_PRESET_ID, "presets": [preset.to_json() for preset in PRESETS]}
        if not json_mode:
            print_presets(result)
        return result

    run_command("video-presets", json_mode, body)
