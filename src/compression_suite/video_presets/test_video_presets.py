"""CLI tests for video-presets: the named video presets, listed for the caller."""

import json

from click.testing import Result
from typer.testing import CliRunner

from compression_suite.cli import app

runner = CliRunner()


def _invoke(*args: str) -> Result:
    return runner.invoke(app, ["video-presets", *args])


def _result(result: Result) -> dict:
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["schema_version"] == 2
    assert document["ok"] is True
    assert document["command"] == "video-presets"
    return document["result"]


def test_lists_the_three_starting_presets_with_their_exact_settings():
    presets = [
        {key: value for key, value in preset.items() if key != "crf_bounds"}
        for preset in _result(_invoke("--json"))["presets"]
    ]
    assert presets == [
        {
            "id": "default",
            "label": "Défaut",
            "container": "mp4",
            "video": {"encoder": "x265", "speed": "slow", "crf": 32},
            "audio": {"codec": "opus", "bitrate_kbps": 32},
        },
        {
            "id": "screen",
            "label": "Écran / cours",
            "container": "mp4",
            "video": {"encoder": "x265", "speed": "slow", "crf": 35},
            "audio": {"codec": "opus", "bitrate_kbps": 32},
        },
        {
            "id": "dance",
            "label": "Danse",
            "container": "mp4",
            "video": {"encoder": "x265", "speed": "slow", "crf": 45},
            "audio": None,
        },
    ]


def test_gives_the_crf_bounds_of_each_preset():
    for preset in _result(_invoke("--json"))["presets"]:
        assert preset["crf_bounds"] == {"min": 0, "max": 51}


def test_names_the_default_preset():
    assert _result(_invoke("--json"))["default_preset"] == "default"


def test_without_json_lists_the_presets_for_a_human():
    result = _invoke()
    assert result.exit_code == 0, result.output
    assert "screen: Écran / cours (mp4, x265 slow, CRF 35 (0 to 51), opus 32k)" in result.stdout
    assert "dance: Danse (mp4, x265 slow, CRF 45 (0 to 51), no audio)" in result.stdout


def test_unknown_option_is_a_json_usage_error():
    result = _invoke("--json", "--bogus")
    assert result.exit_code == 2
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert document["error"]["code"] == "usage_error"
