"""CLI tests for the metadata diff of compress-video and the important fields inspect gives a video: the pinned ffmpeg,
ffprobe and exiftool run for real, on videos fabricated at test time (see test_compress_video)."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.compress_image.samples.make_motion_photo_samples import IMAGE_UTC_DATA, seft_trailer
from compression_suite.compress_video import main
from compression_suite.compress_video.test_compress_video import CREATED, MTIME_NS, _copy, _make_video, _tags, _tool
from compression_suite.utils.metadata_diff import ExpectedLoss

runner = CliRunner()

MTIME_ISO = "2022-08-09T10:11:12.654321+00:00"  # MTIME_NS

# The movie's creation date, stored in UTC, with its offset
CREATED_UTC = f"{CREATED}+00:00"

VIDEO_IMPORTANT_KEYS = ["capture_date", "gps", "camera", "dimensions", "rotation"]

# exiftool's reading of the sample's GPS (`©xyz`), as the diff gives it
GPS_COORDINATES = "48 deg 51' 30.24\" N, 2 deg 17' 40.20\" E, 35 m Above Sea Level"


@pytest.fixture(scope="module")
def phone_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A phone-like HEVC video (rotation, GPS, creation date, audio), made once: tests copy it with `_copy`."""
    return _make_video(tmp_path_factory.mktemp("samples") / "phone.mp4")


def _compress(source: Path, output: Path, *args: str, preset: str = "default") -> dict:
    """Run compress-video --json and return `result.metadata`."""
    result = runner.invoke(app, ["compress-video", str(source), "--preset", preset, "-o", str(output), "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["metadata"]


def _important(metadata: dict) -> dict:
    return {field["key"]: field for field in metadata["important"]}


def _expected(metadata: dict) -> dict:
    """The expected losses, tag to reason."""
    return {loss["tag"]: loss["reason"] for loss in metadata["expected_losses"]}


def _exiftool(path: Path, *assignments: str) -> None:
    subprocess.run([_tool("exiftool"), "-q", "-overwrite_original", *assignments, str(path)], check=True)


def _ffmpeg(*args: str) -> None:
    subprocess.run([_tool("ffmpeg"), "-v", "error", "-y", *args], check=True)


TESTSRC = ("-f", "lavfi", "-i", "testsrc=size=64x32:rate=10:duration=1")
SINE = ("-f", "lavfi", "-i", "sine=duration=1")


# --- Shape and important fields ----------------------------------------------------


def test_result_holds_the_metadata_diff_of_compress_image_shape(phone_video: Path, tmp_path: Path):
    metadata = _compress(_copy(phone_video, tmp_path), tmp_path / "out.mp4")
    assert list(metadata) == ["important", "unexpected_losses", "expected_losses", "added", "before", "after"]
    assert metadata["before"]["QuickTime:CreateDate"] == CREATED
    assert metadata["after"]["Track1:CompressorID"] == "hvc1"
    assert not any(tag.split(":")[0] in ("System", "Composite", "ExifTool") for tag in metadata["after"])


def test_important_fields_of_the_phone_video_are_kept(phone_video: Path, tmp_path: Path):
    metadata = _compress(_copy(phone_video, tmp_path), tmp_path / "out.mp4")
    assert [field["key"] for field in metadata["important"]] == VIDEO_IMPORTANT_KEYS
    important = _important(metadata)
    assert important["capture_date"] == {
        "key": "capture_date",
        "before": CREATED_UTC,
        "after": CREATED_UTC,
        "source": "QuickTime:CreateDate",
        "status": "same",
    }
    assert important["gps"] == {
        "key": "gps",
        "before": {"coordinates": GPS_COORDINATES},
        "after": {"coordinates": GPS_COORDINATES},
        "source": "UserData:GPSCoordinates",
        "status": "same",
    }
    assert important["rotation"] == {
        "key": "rotation",
        "before": 90,
        "after": 90,
        "source": "Track1:MatrixStructure",
        "status": "same",
    }
    # Stored 64×32, turned a quarter: displayed 32×64
    assert important["dimensions"] == {
        "key": "dimensions",
        "before": {"width": 32, "height": 64},
        "after": {"width": 32, "height": 64},
        "source": "Track1:ImageWidth",
        "status": "same",
    }
    assert important["camera"]["status"] == "absent"


def test_capture_date_is_local_time_with_the_time_zone_a_galaxy_records(phone_video: Path, tmp_path: Path):
    source = _copy(phone_video, tmp_path)
    _exiftool(source, "-Keys:AndroidTimeZone=+0200")  # the offset a Galaxy writes (`samsung.android.utc_offset`)
    metadata = _compress(source, tmp_path / "out.mp4")
    local = "2022:08:09 12:11:12+02:00"  # CREATED, 10:11:12 in UTC
    assert _important(metadata)["capture_date"] == {
        "key": "capture_date",
        "before": local,
        "after": local,
        "source": "QuickTime:CreateDate",
        "status": "same",
    }
    assert {field["key"]: field for field in _inspect_important(source)}["capture_date"]["value"] == local


def test_capture_date_of_a_time_zone_west_of_utc_falls_on_the_previous_day(phone_video: Path, tmp_path: Path):
    source = _copy(phone_video, tmp_path)
    _exiftool(source, "-Keys:AndroidTimeZone=-1130")
    important = {field["key"]: field for field in _inspect_important(source)}
    assert important["capture_date"]["value"] == "2022:08:08 22:41:12-11:30"


def test_phone_video_export_loses_nothing_unexpectedly(phone_video: Path, tmp_path: Path):
    metadata = _compress(_copy(phone_video, tmp_path), tmp_path / "out.mp4")
    assert metadata["unexpected_losses"] == []


# --- Expected losses ---------------------------------------------------------------


def test_encoding_parameters_are_expected_losses_for_reencoding(tmp_path: Path):
    source = _make_video(tmp_path / "plain.mp4", codec="h264")  # H.264 and AAC: codec, rates and brands change
    metadata = _compress(source, tmp_path / "out.mp4")
    expected = _expected(metadata)
    for tag in ("Track1:CompressorID", "Track2:AudioFormat", "Track2:AudioSampleRate", "QuickTime:TimeScale"):
        assert expected[tag] == "réencodage", tag
    assert metadata["unexpected_losses"] == []


def test_audio_dropped_by_the_dance_preset_is_an_expected_loss(phone_video: Path, tmp_path: Path):
    metadata = _compress(_copy(phone_video, tmp_path), tmp_path / "out.mp4", preset="dance")
    expected = _expected(metadata)
    assert expected["Track2:AudioFormat"] == "piste audio retirée par le préréglage « Danse »"
    assert expected["Track2:TrackCreateDate"] == "piste audio retirée par le préréglage « Danse »"
    assert not any(tag.startswith("Track2:") for tag in metadata["after"])
    assert metadata["unexpected_losses"] == []


def test_the_movie_shortened_to_its_video_when_a_longer_track_is_dropped_is_an_expected_loss(tmp_path: Path):
    source = tmp_path / "long_audio.mp4"  # the sound lasts longer than the picture, as a phone records it
    _ffmpeg(*TESTSRC, "-f", "lavfi", "-i", "sine=duration=1.5", "-c:v", "libx264", "-c:a", "aac", str(source))
    metadata = _compress(source, tmp_path / "out.mp4", preset="dance")
    assert _expected(metadata)["QuickTime:Duration"] == "durée du film : celle des pistes gardées"
    assert "Track1:TrackDuration" not in _expected(metadata)  # the video's own duration is still compared
    assert metadata["unexpected_losses"] == []


def _late_audio(path: Path) -> Path:
    """A phone-like HEVC and AAC clip whose sound starts 50 ms after the picture: its audio track starts with an empty
    edit, then plays its media from the start (an edit list as a Galaxy writes it)."""
    _ffmpeg(
        *("-f", "lavfi", "-i", "testsrc=size=64x32:rate=30:duration=1"),
        *("-itsoffset", "0.05", "-f", "lavfi", "-i", "sine=duration=0.9:sample_rate=48000"),
        *("-c:v", "libx265", "-x265-params", "log-level=error", "-tag:v", "hvc1", "-c:a", "aac", str(path)),
    )
    return path


def test_audio_starting_after_the_video_keeps_its_start_and_its_end(tmp_path: Path):
    source = _late_audio(tmp_path / "late.mp4")
    output = tmp_path / "out.mp4"
    metadata = _compress(source, output)
    assert metadata["unexpected_losses"] == []
    # To the millisecond, finer than the diff's rounding: ffmpeg alone ends it 20 ms late
    assert _tags(output, "Track2:TrackDuration")["Track2:TrackDuration"] == pytest.approx(
        _tags(source, "Track2:TrackDuration")["Track2:TrackDuration"], abs=0.001
    )
    start = float(_stream_start(output))
    assert start + 312 / 48000 == pytest.approx(float(_stream_start(source)), abs=0.001)  # Opus pre-skip before it


def _stream_start(video: Path) -> str:
    """The start time of the audio stream, as ffprobe reads it (its edit list applied)."""
    return subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=start_time"]
        + ["-of", "csv=p=0", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def test_a_lost_creation_date_gps_or_rotation_is_never_expected(
    phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    copy_metadata = main.copy_metadata

    def damaging_copy(exiftool: Path, path: Path, encoded: Path, destination: int, dates: list[str]) -> None:
        damage = ["-UserData:GPSCoordinates=", "-QuickTime:CreateDate=2000:01:01 00:00:00", "-Rotation=0"]
        copy_metadata(exiftool, path, encoded, destination, [*dates, *damage])

    monkeypatch.setattr(main, "copy_metadata", damaging_copy)
    monkeypatch.setattr(main, "export_expected_losses", lambda *args: [ExpectedLoss("*", "tout est attendu")])
    metadata = _compress(_copy(phone_video, tmp_path), tmp_path / "out.mp4")
    unexpected = {loss["tag"]: loss["kind"] for loss in metadata["unexpected_losses"]}
    assert unexpected == {
        "QuickTime:CreateDate": "changed",
        "UserData:GPSCoordinates": "removed",
        "Track1:MatrixStructure": "changed",
    }
    important = _important(metadata)
    assert [important[key]["status"] for key in ("capture_date", "gps", "rotation")] == ["changed", "lost", "changed"]


def test_audio_downmixed_for_opus_is_an_expected_loss_with_its_layout(tmp_path: Path):
    source = tmp_path / "surround.mov"  # PCM in a MOV: any layout fits
    _ffmpeg(
        *TESTSRC,
        *("-f", "lavfi", "-i", "sine=duration=1,aformat=channel_layouts=2.1"),
        *("-c:v", "libx264", "-c:a", "pcm_s16le", str(source)),
    )
    metadata = _compress(source, tmp_path / "out.mp4")
    assert _expected(metadata)["Track2:AudioChannels"] == "Opus ne prend pas la disposition 2.1 : piste mixée en stereo"
    assert "Track2:AudioChannels" not in {loss["tag"] for loss in metadata["unexpected_losses"]}


def test_tracks_the_export_does_not_keep_are_expected_losses_and_its_own_tracks_are_added(tmp_path: Path):
    source = tmp_path / "timecode.mp4"  # video, audio, then a timecode (data) track
    _ffmpeg(*TESTSRC, *SINE, "-c:v", "libx264", "-c:a", "aac", "-timecode", "01:00:00:00", str(source))
    metadata = _compress(source, tmp_path / "out.mp4", preset="dance")
    expected = _expected(metadata)
    assert expected["Track2:AudioFormat"] == "piste audio retirée par le préréglage « Danse »"
    assert expected["Track3:OtherFormat"].startswith("piste non conservée")
    # ffmpeg writes a timecode track of its own, second in the export: named apart from the original's third track
    added = {entry["tag"]: entry["after"] for entry in metadata["added"]}
    assert (added["Track4:OtherFormat"], added["Track4:TrackID"]) == ("tmcd", 2)
    assert metadata["after"]["Track1:CompressorID"] == "hvc1"
    assert metadata["unexpected_losses"] == []


def _with_track_id(path: Path, track: int, track_id: int) -> Path:
    """Give the track (0 the first) another track ID, as a phone that numbers its tracks with gaps (its own data
    tracks among them) does."""
    data = bytearray(path.read_bytes())
    start = -1
    for _ in range(track + 1):
        start = data.index(b"tkhd", start + 1)
    offset = start + 4 + (20 if data[start + 4] == 1 else 12)  # after the version, flags and dates
    data[offset : offset + 4] = track_id.to_bytes(4, "big")
    path.write_bytes(bytes(data))
    return path


def test_track_ids_given_anew_by_the_export_are_expected_losses(tmp_path: Path):
    source = tmp_path / "gaps.mp4"
    _ffmpeg(*TESTSRC, *SINE, "-c:v", "libx264", "-c:a", "aac", str(source))
    _with_track_id(source, 1, 5)  # the sound is track 5: the export numbers it 2
    metadata = _compress(source, tmp_path / "out.mp4")
    assert (metadata["before"]["Track2:TrackID"], metadata["after"]["Track2:TrackID"]) == (5, 2)
    assert _expected(metadata)["Track2:TrackID"] == "pistes renumérotées par l'export"
    assert metadata["unexpected_losses"] == []


def test_an_odd_size_cropped_by_x265_is_an_expected_loss(tmp_path: Path):
    source = tmp_path / "odd.mp4"  # 4:4:4 holds an odd size
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=65x33:rate=10:duration=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv444p",
        str(source),
    )
    metadata = _compress(source, tmp_path / "out.mp4")
    reason = "largeur ou hauteur impaire : dernière colonne ou ligne retirée (x265 en 4:2:0)"
    assert _expected(metadata)["Track1:ImageWidth"] == reason
    assert _expected(metadata)["Track1:SourceImageHeight"] == reason
    assert _important(metadata)["dimensions"]["after"] == {"width": 64, "height": 32}
    assert metadata["unexpected_losses"] == []


def _box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def _with_samsung_smta(path: Path, model: bytes) -> Path:
    """Add a Samsung `smta` box (the phone's model, as a Galaxy writes it) in a `udta` box at the end of `moov`. The
    `moov` box must be the file's last: growing it then moves no offset."""
    data = path.read_bytes()
    start = 0
    while start < len(data):
        size, kind = int.from_bytes(data[start : start + 4], "big"), data[start + 4 : start + 8]
        if kind == b"moov":
            break
        start += size
    assert start + size == len(data), "moov is not the last box"
    smta = _box(b"smta", b"\0" * 4 + _box(b"saut", b"\0" * 6) + _box(b"mdln", model))
    udta = _box(b"udta", smta)
    path.write_bytes(data[:start] + (size + len(udta)).to_bytes(4, "big") + data[start + 4 :] + udta)
    return path


def _decoding_errors(video: Path) -> str:
    """What ffmpeg reports while decoding every stream to the end, empty for a sound file."""
    return subprocess.run(
        [_tool("ffmpeg"), "-v", "error", "-i", str(video), "-map", "0", "-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr


def _video_frames(video: Path) -> str:
    """The video stream's packet count, as ffprobe reads it (the audio is encoded anew, in packets of its own)."""
    return subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-select_streams", "v", "-count_packets"]
        + ["-show_entries", "stream=nb_read_packets"]
        + ["-of", "csv=p=0", str(video)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def test_samsung_model_box_is_put_back_and_the_video_still_plays(tmp_path: Path):
    source = tmp_path / "galaxy.mp4"  # written by ffmpeg: `moov` last
    _ffmpeg(*TESTSRC, *SINE, "-c:v", "libx265", "-x265-params", "log-level=error", "-c:a", "aac", str(source))
    _with_samsung_smta(source, b"SM-X000Z")  # a made-up model code
    output = tmp_path / "out.mp4"
    metadata = _compress(source, output)
    camera = _important(metadata)["camera"]
    assert (camera["after"], camera["status"], camera["source"]) == (
        {"model": "SM-X000Z"},
        "same",
        "Samsung:SamsungModel",
    )
    assert metadata["unexpected_losses"] == []
    assert not any(tag.startswith("Samsung:") for tag in _expected(metadata))
    # The box grows the index written first: the media data moved, and every chunk offset with it
    assert _decoding_errors(output) == ""
    assert _video_frames(output) == _video_frames(source)


def test_samsung_model_box_that_cannot_be_put_back_is_an_expected_loss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "galaxy.mp4"
    _ffmpeg(*TESTSRC, "-c:v", "libx265", "-x265-params", "log-level=error", str(source))
    _with_samsung_smta(source, b"SM-X000Z")

    def overflowing(path: Path, added: list[bytes]) -> None:
        raise ValueError("a chunk offset no longer fits in 32 bits")  # an output close to 4 GiB

    monkeypatch.setattr(main, "add_user_data_boxes", overflowing)
    output = tmp_path / "out.mp4"
    metadata = _compress(source, output)
    assert _expected(metadata)["Samsung:SamsungModel"] == (
        "boîte Samsung « smta » : ne peut être remise, la vidéo dépasserait les 4 Go qu'adressent ses index"
    )
    assert _important(metadata)["camera"]["status"] == "lost"
    assert metadata["unexpected_losses"] == []
    assert _decoding_errors(output) == ""


def test_another_samsung_field_lost_is_unexpected(tmp_path: Path):
    source = tmp_path / "slow_motion.mp4"  # a Samsung `sefd` box after the media, as a slow-motion clip has
    _ffmpeg(*TESTSRC, "-c:v", "libx265", "-x265-params", "log-level=error", str(source))
    zone = seft_trailer([(IMAGE_UTC_DATA, "Image_UTC_Data", b"1718445600000")])
    source.write_bytes(source.read_bytes() + _box(b"sefd", zone))
    metadata = _compress(source, tmp_path / "out.mp4")
    assert [(loss["tag"], loss["kind"]) for loss in metadata["unexpected_losses"]] == [("Samsung:TimeStamp", "removed")]


def test_a_relative_input_named_like_an_option_is_read_as_a_file(
    phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _copy(phone_video, tmp_path).rename(tmp_path / "-clip.mp4")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["compress-video", "--preset", "default", "-o", "out.mp4", "--json", "--", "-clip.mp4"])
    assert result.exit_code == 0, result.output
    metadata = json.loads(result.stdout)["result"]["metadata"]
    assert metadata["before"]["QuickTime:CreateDate"] == CREATED
    assert metadata["unexpected_losses"] == []


# --- inspect -----------------------------------------------------------------------


def _inspect_important(path: Path) -> list[dict]:
    result = runner.invoke(app, ["inspect", str(path), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["result"]["important"]


def test_inspect_gives_a_video_the_important_fields_of_its_export(phone_video: Path, tmp_path: Path):
    assert _inspect_important(_copy(phone_video, tmp_path)) == [
        {"key": "capture_date", "value": CREATED_UTC, "source": "QuickTime:CreateDate"},
        {"key": "gps", "value": {"coordinates": GPS_COORDINATES}, "source": "UserData:GPSCoordinates"},
        {"key": "camera", "value": None, "source": None},
        {"key": "dimensions", "value": {"width": 32, "height": 64}, "source": "Track1:ImageWidth"},
        {"key": "rotation", "value": 90, "source": "Track1:MatrixStructure"},
    ]


def test_inspect_gives_an_undated_video_the_capture_date_of_its_mtime(tmp_path: Path):
    source = _make_video(tmp_path / "plain.mp4", codec="h264", phone_metadata=False)
    _exiftool(source, "-QuickTime:CreateDate=0000:00:00 00:00:00")  # never set, as a phone video edited by an app
    os.utime(source, ns=(MTIME_NS, MTIME_NS))
    important = {field["key"]: field for field in _inspect_important(source)}
    assert important["capture_date"] == {"key": "capture_date", "value": MTIME_ISO, "source": "file_mtime"}
    assert important["rotation"] == {"key": "rotation", "value": 0, "source": "Track1:MatrixStructure"}


def test_without_json_unexpected_losses_are_named_on_stderr(
    phone_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    copy_metadata = main.copy_metadata

    def gps_lost(exiftool: Path, path: Path, encoded: Path, destination: int, dates: list[str]) -> None:
        copy_metadata(exiftool, path, encoded, destination, [*dates, "-UserData:GPSCoordinates="])

    monkeypatch.setattr(main, "copy_metadata", gps_lost)
    output = tmp_path / "out.mp4"
    result = runner.invoke(
        app, ["compress-video", str(_copy(phone_video, tmp_path)), "--preset", "default", "-o", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert "Unexpected metadata losses: UserData:GPSCoordinates" in result.stderr
    assert result.stdout == ""
