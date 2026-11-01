"""Tests of the output writer shared by the video commands, at its seam: `atomic_output`."""

import os
from pathlib import Path

import pytest

from compression_suite.utils.ffmpeg_output import atomic_output


@pytest.mark.parametrize("overwrite", [False, True], ids=["", "overwrite"])
def test_a_folder_removed_by_a_parallel_call_before_naming_is_made_again(tmp_path: Path, overwrite: bool):
    output = tmp_path / "new" / "deeper" / "out.mp4"

    def other_call_cleans_up() -> None:
        # Another call created these folders too, was interrupted, and removed them: they looked empty
        output.parent.rmdir()
        output.parent.parent.rmdir()

    with atomic_output(output, overwrite, other_call_cleans_up) as written:
        os.write(written.fd, b"complete output")
    assert output.read_bytes() == b"complete output"
    assert [path.name for path in output.parent.iterdir()] == ["out.mp4"]  # no temporary name left
