"""Core logic for preview: a JPEG of a HEIC or HEIF, for display in a browser only.

The HEIC itself is only read, never modified. pillow-heif (pinned by the lockfile) decodes it, not Ubuntu's
heif-convert. It is called directly rather than registered as a Pillow plugin: registering it would let Pillow decode
HEIC everywhere else too, changing what inspect reports.
"""

import io
from pathlib import Path
from typing import Any

import pillow_heif

from compression_suite.compress_image.main import validate_input_file, write_atomically
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.formats import detect_file_format

PREVIEWABLE_FORMATS = ("heic", "heif")

JPEG_QUALITY = 90


def check_format(detected_format: str) -> None:
    """Reject any format preview doesn't convert."""
    if detected_format not in PREVIEWABLE_FORMATS:
        raise CommandError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"Unsupported format: {detected_format} (supported: {', '.join(PREVIEWABLE_FORMATS)})",
            {"detected_format": detected_format},
        )


def check_output(output_path: Path, overwrite: bool) -> None:
    """Refuse to write over a folder, or over an existing file without `--overwrite`."""
    if output_path.is_dir():
        raise CommandError(ErrorCode.OUTPUT_EXISTS, f"Output path is a directory: {output_path}.")
    if output_path.exists() and not overwrite:
        raise CommandError(
            ErrorCode.OUTPUT_EXISTS,
            f"Output file already exists: {output_path}. Use --overwrite to replace.",
        )


def heic_to_jpeg(input_path: Path) -> tuple[bytes, int, int]:
    """Decode the primary image and encode it as a JPEG, at full resolution, with its ICC profile.

    libheif applies the HEIF transformations (`irot`, `imir`) to the pixels and pillow-heif resets the EXIF
    orientation, so nothing is left for a viewer to rotate. An HDR photo gives its SDR base image: pillow-heif
    brings 10 and 12 bits down to 8 and ignores the auxiliary images, the Apple gain map among them.
    """
    image = pillow_heif.open_heif(input_path).to_pillow()
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")  # JPEG has no alpha
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY, icc_profile=image.info.get("icc_profile"))
    return buffer.getvalue(), image.width, image.height


def preview(input_file: str, output: str, overwrite: bool) -> dict[str, Any]:
    """Write the JPEG preview of a HEIC or HEIF and return the `result` object of the JSON contract."""
    input_path = validate_input_file(input_file)
    check_format(detect_file_format(input_path))
    output_path = Path(output)
    check_output(output_path, overwrite)
    jpeg, width, height = heic_to_jpeg(input_path)
    write_atomically(output_path, jpeg, lambda _: None)
    return {"input": input_file, "output": output, "width": width, "height": height, "size_bytes": len(jpeg)}
