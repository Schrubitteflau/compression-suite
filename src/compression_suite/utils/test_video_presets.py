"""Tests of the shared video presets module, as the commands that encode use it.

No command encodes yet: its interface is tested here, the encoding with the pinned ffmpeg for real.
"""

import json
import subprocess
from pathlib import Path

import pytest

from compression_suite.utils.contract import CommandError
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.video_presets import resolve_preset


def _usage_error(preset_id: str, crf: int | None = None) -> CommandError:
    try:
        resolve_preset(preset_id, crf)
    except CommandError as error:
        assert error.code.value == "usage_error"
        assert error.code.exit_code == 2
        return error
    raise AssertionError(f"resolve_preset({preset_id!r}, {crf!r}) raised nothing")


def test_a_preset_keeps_its_own_crf_unless_another_is_given():
    assert resolve_preset("screen").crf == 35
    assert resolve_preset("screen", 40).crf == 40
    assert resolve_preset("screen", 40).id == "screen"


def test_the_crf_bounds_are_accepted():
    assert resolve_preset("default", 0).crf == 0
    assert resolve_preset("default", 51).crf == 51


def test_an_unknown_preset_is_a_usage_error():
    error = _usage_error("tiktok")
    assert error.details == {"preset": "tiktok", "known_presets": ["default", "screen", "dance"]}


@pytest.mark.parametrize("crf", [-1, 52])
def test_a_crf_out_of_bounds_is_a_usage_error(crf: int):
    error = _usage_error("default", crf)
    assert error.details == {"crf": crf, "crf_min": 0, "crf_max": 51}


@pytest.mark.parametrize("crf", [True, 32.5, "40", float("nan")])
def test_a_crf_other_than_a_plain_integer_is_a_usage_error(crf: object):
    error = _usage_error("default", crf)  # ty: ignore[invalid-argument-type]
    assert "integer" in error.message
    assert json.loads(json.dumps(error.details, allow_nan=False))["crf_min"] == 0


def _run(*args: str) -> None:
    subprocess.run([str(resolve_tool("ffmpeg")), "-v", "error", "-y", *args], check=True)


def _make_source(tmp_path: Path, size: str = "64x64", pix_fmt: str = "yuv420p", hlg: bool = False) -> Path:
    """Fabricate a half-second lossless clip (FFV1 in Matroska) with a sine tone, at this size and pixel format."""
    source = tmp_path / f"source-{size}-{pix_fmt}{'-hlg' if hlg else ''}.mkv"
    colour = ",setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc" if hlg else ""
    _run(
        *("-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration=0.5"),
        *("-f", "lavfi", "-i", "sine=frequency=440:duration=0.5"),
        *("-vf", f"format={pix_fmt}{colour}", "-c:v", "ffv1", "-c:a", "flac", str(source)),
    )
    return source


def _encode(source: Path, preset_id: str, filters: tuple[str, ...] = ()) -> list[dict]:
    """Encode `source` with a preset and the pinned ffmpeg; return the output's streams, read by ffprobe."""
    output = source.with_name(f"{source.stem}-{preset_id}.mp4")
    _run("-i", str(source), *resolve_preset(preset_id).ffmpeg_args(filters), str(output))
    probe = subprocess.run(
        [str(resolve_tool("ffprobe")), "-v", "error", "-show_streams", "-of", "json", str(output)],
        check=True,
        capture_output=True,
    )
    return json.loads(probe.stdout)["streams"]


def test_the_default_preset_encodes_hevc_tagged_for_mp4_players_and_opus(tmp_path: Path):
    streams = _encode(_make_source(tmp_path), "default")
    assert [(s["codec_type"], s["codec_name"]) for s in streams] == [("video", "hevc"), ("audio", "opus")]
    assert streams[0]["codec_tag_string"] == "hvc1"
    assert (streams[0]["width"], streams[0]["height"]) == (64, 64)  # no reduction of definition
    assert (streams[0]["profile"], streams[0]["pix_fmt"]) == ("Main", "yuv420p")


def test_the_dance_preset_drops_the_audio(tmp_path: Path):
    streams = _encode(_make_source(tmp_path), "dance")
    assert [s["codec_type"] for s in streams] == ["video"]


def test_an_odd_definition_is_cropped_to_the_even_one_below(tmp_path: Path):
    video = _encode(_make_source(tmp_path, size="65x33"), "default")[0]
    assert (video["width"], video["height"]) == (64, 32)


@pytest.mark.parametrize("pix_fmt", ["rgb24", "gbrp", "yuv444p"])
def test_an_8_bit_rgb_or_444_source_becomes_420_8_bit_main(tmp_path: Path, pix_fmt: str):
    video = _encode(_make_source(tmp_path, pix_fmt=pix_fmt), "default")[0]
    assert (video["profile"], video["pix_fmt"]) == ("Main", "yuv420p")


def test_a_10_bit_hlg_source_stays_main_10_with_its_colour_tags(tmp_path: Path):
    video = _encode(_make_source(tmp_path, pix_fmt="yuv420p10le", hlg=True), "default")[0]
    assert (video["profile"], video["pix_fmt"]) == ("Main 10", "yuv420p10le")
    assert (video["color_primaries"], video["color_transfer"], video["color_space"]) == (
        "bt2020",
        "arib-std-b67",
        "bt2020nc",
    )


def test_a_caller_filter_is_chained_before_the_preset_ones(tmp_path: Path):
    video = _encode(_make_source(tmp_path), "default", ("scale=33:21",))[0]
    assert (video["width"], video["height"]) == (32, 20)
