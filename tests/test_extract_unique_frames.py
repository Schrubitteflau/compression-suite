import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from compression_suite.cli import app
from compression_suite.extract_unique_frames.main import (
    compute_hash,
    extract_unique_frames_to_folder,
    is_different_from_previous,
)
from compression_suite.extract_unique_frames.manual_selection import snap_rectangle_to_frame_edges
from compression_suite.models.geometry import PixelRectangle
from compression_suite.models.metadata import Metadata


def create_split_screen_video(folder: Path) -> Path:
    frames = []
    for speaker_color, slide_top in (("red", 8), ("blue", 8), ("green", 40)):
        frame = Image.new("RGB", (64, 64), "white")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 0, 31, 63), fill=speaker_color)
        draw.rectangle((40, slide_top, 55, slide_top + 15), fill="black")
        frames.append(frame)

    for index, frame in enumerate(frames):
        frame.save(folder / f"source_{index:03d}.png")

    video_path = folder / "split-screen.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-i",
            str(folder / "source_%03d.png"),
            "-c:v",
            "png",
            str(video_path),
        ],
        check=True,
    )
    return video_path


@pytest.fixture(scope="session")
def split_screen_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return create_split_screen_video(tmp_path_factory.mktemp("split-screen-video"))


def install_fake_opencv(
    monkeypatch: pytest.MonkeyPatch,
    rectangles: list[tuple[int, int, int, int]],
) -> SimpleNamespace:
    selections = iter(rectangles)
    fake_cv2 = SimpleNamespace(
        WINDOW_NORMAL=1,
        WINDOW_KEEPRATIO=2,
        WINDOW_GUI_NORMAL=4,
        named_window_flags=[],
        namedWindow=lambda *_args, **_kwargs: None,
        selectROI=lambda *_args, **_kwargs: next(selections, (0, 0, 0, 0)),
        destroyWindow=lambda *_args, **_kwargs: None,
    )
    fake_cv2.namedWindow = lambda _name, flags: fake_cv2.named_window_flags.append(flags)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    monkeypatch.setenv("DISPLAY", ":test")
    return fake_cv2


def test_comparison_hash_ignores_changes_inside_excluded_region() -> None:
    before = Image.new("RGB", (64, 64), "white")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((0, 0, 31, 63), fill="black")

    excluded_region = PixelRectangle(x=0, y=0, width=32, height=64)

    assert compute_hash(before, [excluded_region]) == compute_hash(after, [excluded_region])


def test_comparison_hash_detects_changes_outside_excluded_region() -> None:
    before = Image.new("RGB", (64, 64), "white")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((40, 16, 55, 47), fill="black")

    excluded_region = PixelRectangle(x=0, y=0, width=32, height=64)

    assert is_different_from_previous(
        compute_hash(after, [excluded_region]),
        compute_hash(before, [excluded_region]),
    )


def test_version_1_metadata_loads_without_comparison_exclusions() -> None:
    metadata = Metadata.model_validate(
        {
            "version": "1.0",
            "frame_changes_count": 1,
            "unique_images_count": 1,
            "timestamps": [{"timestamp": 0.0, "hash": "abc", "image_index": 0}],
            "format": "png",
            "video_info": {"width": 64, "height": 64, "fps": 1.0, "duration": 1.0},
        }
    )

    assert metadata.comparison_exclusion_regions == []


def test_manual_selection_snaps_nearby_edges_to_frame_boundaries() -> None:
    selected_region = PixelRectangle(x=7, y=9, width=116, height=58)

    assert snap_rectangle_to_frame_edges(selected_region, frame_width=128, frame_height=72) == PixelRectangle(
        x=0,
        y=0,
        width=128,
        height=72,
    )


def test_manual_selection_preserves_edges_outside_snap_tolerance() -> None:
    selected_region = PixelRectangle(x=11, y=11, width=106, height=50)

    assert snap_rectangle_to_frame_edges(selected_region, frame_width=128, frame_height=72) == selected_region


@pytest.mark.parametrize("use_webp", [False, True])
@pytest.mark.parametrize("mpdecimate", [False, True])
def test_extraction_deduplicates_motion_inside_excluded_region(
    tmp_path: Path,
    split_screen_video: Path,
    use_webp: bool,
    mpdecimate: bool,
) -> None:
    output_path = tmp_path / "output"

    extract_unique_frames_to_folder(
        str(split_screen_video),
        str(output_path),
        use_webp=use_webp,
        mpdecimate=mpdecimate,
        comparison_exclusion_regions=[PixelRectangle(x=0, y=0, width=32, height=64)],
    )

    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert metadata.frame_changes_count == 2


def test_extraction_records_comparison_exclusions(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"
    excluded_region = PixelRectangle(x=0, y=0, width=32, height=64)

    extract_unique_frames_to_folder(
        str(split_screen_video),
        str(output_path),
        use_webp=False,
        mpdecimate=False,
        comparison_exclusion_regions=[excluded_region],
    )

    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert (metadata.version, metadata.comparison_exclusion_regions) == (
        "1.1",
        [excluded_region],
    )


def test_extraction_preserves_excluded_pixels_in_saved_frames(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    extract_unique_frames_to_folder(
        str(split_screen_video),
        str(output_path),
        use_webp=False,
        mpdecimate=False,
        comparison_exclusion_regions=[PixelRectangle(x=0, y=0, width=32, height=64)],
    )

    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    first_frame = Image.open(output_path / f"{metadata.timestamps[0].hash}.png")
    assert first_frame.getpixel((16, 32)) == (255, 0, 0)


def test_extraction_rejects_out_of_bounds_region_before_creating_output(
    tmp_path: Path,
    split_screen_video: Path,
) -> None:
    output_path = tmp_path / "output"

    with pytest.raises(ValueError, match="outside the 64x64 video bounds"):
        extract_unique_frames_to_folder(
            str(split_screen_video),
            str(output_path),
            use_webp=False,
            mpdecimate=False,
            comparison_exclusion_regions=[PixelRectangle(x=60, y=0, width=8, height=64)],
        )

    assert not output_path.exists()


def test_extraction_allows_excluding_the_entire_frame(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    extract_unique_frames_to_folder(
        str(split_screen_video),
        str(output_path),
        use_webp=False,
        mpdecimate=False,
        comparison_exclusion_regions=[PixelRectangle(x=0, y=0, width=64, height=64)],
    )

    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert metadata.frame_changes_count == 1


def test_cli_accepts_explicit_comparison_exclusion(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--output-format",
            "png",
            "--no-mpdecimate",
            "--ignore-region",
            "0,0,32,64",
            "--ignore-region",
            "60,0,4,4",
        ],
    )

    assert result.exit_code == 0, result.output
    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert metadata.comparison_exclusion_regions == [
        PixelRectangle(x=0, y=0, width=32, height=64),
        PixelRectangle(x=60, y=0, width=4, height=4),
    ]


def test_cli_rejects_invalid_region_before_creating_output(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--ignore-region=-1,0,32,64",
        ],
    )

    assert "Invalid --ignore-region '-1,0,32,64'" in result.output
    assert not output_path.exists()


def test_cli_rejects_manual_and_explicit_regions_together(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--ignore-region",
            "0,0,32,64",
            "--manual-select-ignore-region",
        ],
    )

    assert "--manual-select-ignore-region cannot be used with --ignore-region" in result.output
    assert not output_path.exists()


def test_cli_rejects_selection_timestamp_without_manual_selection(
    tmp_path: Path,
    split_screen_video: Path,
) -> None:
    output_path = tmp_path / "output"

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--selection-timestamp",
            "1.0",
        ],
    )

    assert "--selection-timestamp requires --manual-select-ignore-region" in result.output
    assert not output_path.exists()


def test_cli_manual_selection_prints_and_records_resolved_region(
    tmp_path: Path,
    split_screen_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output"
    fake_cv2 = install_fake_opencv(monkeypatch, [(0, 0, 32, 64)])

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--output-format",
            "png",
            "--no-mpdecimate",
            "--manual-select-ignore-region",
            "--selection-timestamp",
            "1.0",
        ],
        input="n\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "--ignore-region 0,0,32,64" in result.output
    assert "Add another region?" in result.output
    assert "Use these regions?" in result.output
    assert fake_cv2.named_window_flags == [7]
    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert metadata.comparison_exclusion_regions == [PixelRectangle(x=0, y=0, width=32, height=64)]


def test_cli_manual_selection_can_retry_before_extraction(
    tmp_path: Path,
    split_screen_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output"
    install_fake_opencv(monkeypatch, [(5, 5, 20, 20), (0, 0, 32, 64)])

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--output-format",
            "png",
            "--no-mpdecimate",
            "--manual-select-ignore-region",
        ],
        input="n\nn\nn\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "--ignore-region 0,0,25,25" in result.output
    assert result.output.count("Use these regions?") == 2
    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert metadata.comparison_exclusion_regions == [PixelRectangle(x=0, y=0, width=32, height=64)]


def test_cli_manual_selection_can_accumulate_multiple_regions(
    tmp_path: Path,
    split_screen_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output"
    install_fake_opencv(monkeypatch, [(0, 0, 20, 64), (40, 0, 24, 10)])

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--output-format",
            "png",
            "--no-mpdecimate",
            "--manual-select-ignore-region",
        ],
        input="y\nn\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "--ignore-region 0,0,20,64" in result.output
    assert result.output.count("Selected comparison exclusions:") == 2
    metadata = Metadata.model_validate_json((output_path / "metadata.json").read_text())
    assert metadata.comparison_exclusion_regions == [
        PixelRectangle(x=0, y=0, width=20, height=64),
        PixelRectangle(x=40, y=0, width=24, height=10),
    ]


def test_cli_manual_selection_cancels_before_creating_output(
    tmp_path: Path,
    split_screen_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output"
    install_fake_opencv(monkeypatch, [])

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--manual-select-ignore-region",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Selection cancelled; no output was created." in result.output
    assert not output_path.exists()


def test_cli_manual_selection_rejects_timestamp_at_video_end(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--manual-select-ignore-region",
            "--selection-timestamp",
            "3.0",
        ],
    )

    assert "--selection-timestamp must be greater than or equal to 0 and less" in result.output
    assert not output_path.exists()


def test_cli_manual_selection_rejects_non_finite_timestamp(tmp_path: Path, split_screen_video: Path) -> None:
    output_path = tmp_path / "output"

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--manual-select-ignore-region",
            "--selection-timestamp",
            "nan",
        ],
    )

    assert "--selection-timestamp must be a finite number" in result.output
    assert not output_path.exists()


def test_cli_manual_selection_explains_missing_gui_dependency(
    tmp_path: Path,
    split_screen_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output"
    real_import_module = importlib.import_module

    def import_without_opencv(name: str, package: str | None = None):
        if name == "cv2":
            raise ImportError
        return real_import_module(name, package)

    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.setattr(importlib, "import_module", import_without_opencv)
    monkeypatch.setenv("DISPLAY", ":test")

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--manual-select-ignore-region",
        ],
    )

    assert "pip install 'compression-suite[gui]'" in result.output
    assert not output_path.exists()


def test_cli_manual_selection_explains_missing_display(
    tmp_path: Path,
    split_screen_video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "extract-unique-frames",
            str(split_screen_video),
            str(output_path),
            "--manual-select-ignore-region",
        ],
    )

    assert "requires a graphical desktop session" in result.output
    assert not output_path.exists()
