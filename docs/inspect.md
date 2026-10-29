# inspect

Describe any file, compressible or not: real format, MIME type, size, modification date, important fields, full metadata and motion photo signals.

It never contacts Tinify and needs no API key. A format nobody knows is not an error: only a missing or unreadable input (`input_not_found`, exit 23) and a missing pinned exiftool (`tool_missing`, exit 30) make it fail.

## Prerequisites

- **Pinned exiftool**: installed in the project by `just install-tools`, at the version of `pinned-tools.json`
- **Perl**: needed to run exiftool

## Usage

```bash
# Readable report on stdout
compression_suite inspect photo.jpg

# Machine-readable output (one JSON document on stdout)
compression_suite inspect video.mp4 --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | *(required)* | Path to the file to describe (any format) |
| `--json` | `false` | Write a single JSON document on stdout |

## JSON output (`--json`)

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "inspect",
  "result": {
    "path": "photo.jpg",
    "format": "jpeg",
    "mime_type": "image/jpeg",
    "size_bytes": 2154321,
    "mtime": "2024-06-15T10:00:00.123456+00:00",
    "compressible": true,
    "motion_photo": {"detected": false, "signals": []},
    "important": [
      {"key": "capture_date", "value": "2024:06:15 12:00:00", "source": "ExifIFD:DateTimeOriginal"},
      {"key": "color_profile", "value": "DCI-P3 D65 Gamut with sRGB Transfer", "source": "ICC_Profile:ProfileDescription"}
    ],
    "metadata": {"File:FileType": "JPEG", "IFD0:Make": "samsung", "...": "..."}
  }
}
```

- **`format`**: detected on the content, never on the extension: `jpeg`, `png`, `webp`, `gif`, `pdf`, `avif`, `heic`, `heif` as for `compress-image`, else the file type exiftool reads in the content, lowercased (`mp4`, `mov`, `mp3`, `txt`…), else `unknown`.
- **`mime_type`**: the MIME type of that format, exiftool's for the formats it names, else `application/octet-stream`.
- **`mtime`**: ISO 8601, in UTC, to the microsecond.
- **`compressible`**: whether `compress-image` accepts the format (JPEG, PNG, WebP, AVIF), as its own detector reads it, never from exiftool's name. A motion photo stays compressible: `compress-image` refuses it for its video, not for its format.
- **`motion_photo`**: the same detector and signals as [`compress-image`](compress-image.md#motion-photos). The file is mapped rather than read, so a large video costs no memory.
- **`important`**: the same fields, in the same order, as the metadata diff of `compress-image`, with only `value` and `source`. `capture_date` falls back on the file's mtime (`source: "file_mtime"`). `value` and `source` are `null` when nothing gives the field. `transparency` is read on the pixels decoded by Pillow (`source: "pixels"`); both are `null` when Pillow can't decode the file.
- **`metadata`**: every tag read by the pinned exiftool (`exiftool -j -G1 -a`, keyed `Group:Name`), without the ignored groups `System`, `Composite` and `ExifTool`.

Without `--json`, the same description is printed on stdout for a human, and errors on stderr.
