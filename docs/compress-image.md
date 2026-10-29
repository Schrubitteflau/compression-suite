# compress-image

Compress images via the [Tinify API](https://tinypng.com/developers) with selective metadata preservation.

Supports JPEG, PNG, WebP, and AVIF — any format accepted by Tinify. The API accepts files up to 500MB (the 5MB limit only applies to the web interface).

## Prerequisites

- **Pinned exiftool**: installed in the project by `just install-tools`, at the version of `pinned-tools.json`. The exiftool found in `PATH` (e.g. `/usr/bin/exiftool`) is never used
- **Perl**: needed to run exiftool
- **Tinify API key**: get one at https://tinypng.com/developers (500 free compressions/month), then write it in the configuration file (see [the README](../README.md#tinify-api-key)) or pass it with `--api-key`

## Usage

```bash
# File to file, preserving metadata (default)
compression_suite compress-image photo.jpg --output compressed.jpg

# Strip metadata
compression_suite compress-image photo.jpg --output stripped.jpg --metadata=strip

# stdin to stdout (for piping)
cat photo.jpg | compression_suite compress-image --metadata=strip > compressed.jpg

# Override the 15MB safety limit
compression_suite compress-image huge.png --output out.png --disable-hard-limit

# Compress a motion photo anyway, losing its video
compression_suite compress-image motion.jpg --output out.jpg --allow-motion-photo-loss

# Machine-readable output (one JSON document on stdout, --output is required)
compression_suite compress-image photo.jpg --output compressed.jpg --json

# Another key than the configuration file's
compression_suite compress-image photo.jpg --output compressed.jpg --api-key=OTHER_KEY
```

The key comes from `--api-key` if given, else from `tinify_api_key` in `~/.config/compression-suite/config.toml` (`$XDG_CONFIG_HOME/compression-suite/config.toml` when set to an absolute path). An empty `--api-key` (an unset variable in `--api-key="$KEY"`) is a missing key: the file is not used instead. No other environment variable is read, and the key never appears in any output. A configuration file on which the group or the others have any right (anything but 600 or stricter) gives a warning on stderr.

## Options

| Option | Default | Description |
|---|---|---|
| `INPUT_FILE` | stdin | Path to input image file (omit to read from stdin) |
| `--output`, `-o` | stdout | Path to output file (omit to write to stdout) |
| `--api-key` | configuration file | Tinify API key, wins over `tinify_api_key` in the configuration file |
| `--metadata` | `keep` | `keep` (preserve via exiftool) or `strip` |
| `--overwrite` | `false` | Overwrite output file if it exists |
| `--disable-hard-limit` | `false` | Bypass the 15MB input size limit |
| `--allow-motion-photo-loss` | `false` | Compress a motion photo anyway, losing its video |
| `--json` | `false` | Write a single JSON document on stdout (requires `--output`) |
| `--verbose`, `-v` | `false` | Enable verbose logging |

## Metadata handling

### `--metadata=keep` (default)

Tinify keeps no metadata at all: no EXIF, no ICC profile, no PNG text block, no Samsung trailer. After the compression, metadata from the original image is copied back to the compressed file using exiftool. A blacklist approach is used — all tags are copied (`-all:all`, plus the ICC profile, the IFD0 dimensions, the Interop tags and the XMP toolkit `XMP-x:XMPToolkit`, which exiftool only copies when named: it would otherwise name itself as the toolkit of the XMP it rewrites) except those that become invalid after compression:

- **ThumbnailImage**: the EXIF thumbnail shows the original image, it is removed on purpose
- **Compression**: belongs to the thumbnail

This preserves camera info (Make, Model, ISO, Exposure, etc.), dates, GPS and all other tags. The orientation is kept unless Tinify already applied it, and the colour profile unless Tinify converted the colours to sRGB (see below). What can't be kept is listed in the metadata diff below.

### Orientation

Tinify applies the EXIF orientation to the pixels it returns: a S23 portrait stored 4000×3000 with `Rotate 90 CW` comes back 3000×4000, a `Mirror horizontal` JPEG comes back mirrored (both seen on real calls, 0.4/255 apart from the original as displayed). Copied back, the orientation would be applied a second time and the photo displayed lying down.

So with `--metadata=keep`, when the original has an orientation other than normal, `compress-image` compares the compressed pixels with the original's, as stored and as displayed (on small grayscale copies, `utils/orientation.py`). When they show the original as displayed, and are closer to it than to the original as stored:

- the orientation is written as normal (`Horizontal (normal)`) in `IFD0:Orientation` and `XMP-tiff:Orientation`, for those the original has;
- `IFD0:ImageWidth`/`ImageHeight` and `ExifIFD:ExifImageWidth`/`ExifImageHeight`, when the original has them, take the size of the compressed pixels: they describe the stored image, and left at 4000×3000 they would contradict pixels now stored 3000×4000.

Otherwise (pixels as stored, pixels that can't be told apart, or an image Pillow can't decode), the orientation is copied back like any other tag; an output then displayed otherwise is reported (`pixels:orientation`, below).

### Colours

Tinify converts the colours to sRGB with the ICC profile, and returns no profile: the P3 pixels of a S23 photo ("DCI-P3 D65 Gamut with sRGB Transfer") come back in sRGB, for a JPEG, PNG, WebP or AVIF (seen on real calls). Copied back, the P3 profile would make a viewer that manages colours read these sRGB pixels as P3: oversaturated, redder colours. The conversion to sRGB, reduced gamut included, is accepted.

So with `--metadata=keep`, when the original has an ICC profile, `compress-image` compares the compressed pixels, converted to sRGB with their own profile if any, with the original's, converted to sRGB with its profile and read as sRGB without it (on small RGB copies, `utils/colors.py`). When they show the original's colours converted, at least as closely as the original read as sRGB (a photo of dull colours, which its profile barely changes, is converted too), the profile is not copied back: the output is in sRGB, without profile. Its loss, the `ICC*` groups, is expected, with the reason « couleurs converties en sRGB par Tinify (gamut réduit) »; in an AVIF, so is the sRGB declared by Tinify in place of unspecified primaries and transfer (`QuickTime:ColorPrimaries`, `QuickTime:TransferCharacteristics`).

Otherwise (colours left as they were, or an image Pillow can't decode or whose profile it can't apply), the profile is copied back like any other tag; colours then displayed otherwise are reported (`pixels:colors`, below). A profile that converts nothing (sRGB) may be either copied back or dropped: the colours are the same.

Known limit: the colours are compared on 32×32 copies, by the mean of each channel. A change confined to a small part of the image (a saturated object on a neutral background) may average out below the threshold.

### `--metadata=strip`

The Tinify API output is used as-is, with no metadata preservation. Only file-level attributes remain (dimensions, encoding process, color space). The metadata diff is still produced: every loss is expected, with the reason "métadonnées retirées à la demande", except a change of dimensions, a lost transparency, or an output displayed otherwise (picture or colours), which are never expected.

### Metadata diff (`--json`)

`result.metadata` compares the tags of the original and of the compressed file, read by the pinned exiftool on every group (`exiftool -j -G1 -a`, tags keyed `Group:Name`):

```json
{
  "important": [
    {"key": "capture_date", "before": "2024:06:15 12:00:00", "after": "2024:06:15 12:00:00", "source": "ExifIFD:DateTimeOriginal", "status": "same"},
    {"key": "color_profile", "before": "DCI-P3 D65 Gamut with sRGB Transfer", "after": null, "source": "ICC_Profile:ProfileDescription", "status": "lost"}
  ],
  "unexpected_losses": [{"tag": "PNG:ImageWidth", "kind": "changed", "before": 320, "after": 160}],
  "expected_losses": [{"tag": "Samsung:TimeStamp", "before": "2024:06:15 12:00:00.918+02:00", "reason": "zone Samsung de fin de fichier : retirée par Tinify, exiftool ne sait pas la réécrire"}],
  "added": [{"tag": "JFIF:JFIFVersion", "after": 1.01}],
  "before": {"IFD0:Make": "samsung", "...": "..."},
  "after": {"IFD0:Make": "samsung", "...": "..."}
}
```

- **Ignored groups**: `System` (file name, size, file system dates and permissions), `Composite` (values computed by exiftool) and `ExifTool` (version, warnings). They are not in `before` and `after` either.
- **`important`**: always the same fields, in this order, even when intact. `status` is `same`, `lost`, `changed` or `absent` (missing on both sides); `source` is the tag the value comes from.
  - `capture_date`: `ExifIFD:DateTimeOriginal`, else the XMP or PNG date, else the file's mtime (`source: "file_mtime"`, file to file only)
  - `gps`: `{latitude, latitude_ref, longitude, longitude_ref, altitude, altitude_ref}` (the parts present)
  - `camera`: `{make, model}`
  - `dimensions`: `{width, height}` as displayed, orientation applied: a portrait stored 4000×3000 with `Rotate 90 CW` is `{width: 3000, height: 4000}`
  - `orientation`
  - `color_profile`: description of the ICC profile, `null` if none. `lost` when Tinify converted the colours to sRGB: the colours themselves are checked on the pixels (`pixels:colors`)
  - `transparency`: whether at least one pixel is not fully opaque, read on the pixels decoded by Pillow (`source: "pixels"`), for every format. `before` or `after` is `null`, and `status` is `unverified`, when Pillow can't decode that file. `lost` means a real transparency disappeared
- **`expected_losses`** (`tag`, `before`, `reason`): losses decided by compression-suite, declared in `compress_image/main.py` next to the exiftool exclusions:
  - the EXIF thumbnail (`IFD1:*`) and its `Compression`
  - the fields of the Samsung end-of-file trailer (the `Samsung:` tags of exiftool's `Samsung::Trailer` table, read in the `SEFT` zone), which Tinify drops and exiftool can't write. Samsung's EXIF maker notes share the `Samsung` group but are copied back, so their loss stays unexpected
  - the encoding parameters Tinify chooses anew ("réencodage"): every `File` field but the dimensions, and the PNG and AVIF encoding fields (bit depth, colour type, palette…)
  - when Tinify applied the orientation to the pixels, the orientation and the stored size tags rewritten (see [Orientation](#orientation)), with the reason « orientation appliquée aux pixels »
  - when Tinify converted the colours to sRGB, the ICC profile (`ICC*` groups) and, in an AVIF, its sRGB primaries and transfer (see [Colours](#colours)), with the reason « couleurs converties en sRGB par Tinify (gamut réduit) »
  - the stored width and height (`File:ImageWidth`…) swapped with each other by a quarter turn of the pixels, the displayed dimensions kept, with the reason « pixels pivotés selon l'orientation : l'image affichée garde ses dimensions »
- **Never expected**, whatever `--metadata`: a change of the dimensions as displayed (each file's dimensions turned by its own orientation), an output a viewer displays otherwise than the original (picture or colours), and a change of the alpha tags (`PNG:ColorType`, `PNG:Transparency`, `RIFF:WebP_Flags`, `RIFF:Alpha*`, `QuickTime:AuxiliaryImageType`) unless the original is known to be opaque or the output known to be transparent. Tinify drops the alpha channel of an image without any transparent pixel, such as a gnome-screenshot capture: that is re-encoding. A real transparency lost, or one that couldn't be checked, is not.
- **`unexpected_losses`** (`tag`, `kind`, `before`, `after`): any other tag that disappeared (`kind: "removed"`, `after: null`) or whose value changed (`kind: "changed"`). A real transparency lost, or that couldn't be checked, is also listed under the synthetic tag `pixels:transparency` (`kind: "removed"` or `"unverified"`, `before`/`after` as in the `transparency` field), since an output can keep its alpha tags with every pixel made opaque. An original known to be opaque gives no such entry. An original with an orientation other than normal whose output a viewer displays otherwise (turned or mirrored, each with its own orientation: an orientation applied twice, for instance) is listed under the synthetic tag `pixels:orientation` (`kind: "changed"`, `before`/`after` the orientation of each file); this is read on the pixels, since a mirror applied twice changes no dimension. An output whose colours, converted to sRGB with its own profile (read as sRGB without one), differ from the original's converted the same way by more than the noise of compression (1.5/255 on the mean of a channel) is listed under the synthetic tag `pixels:colors` (`kind: "changed"`, `before`/`after` the profile description of each file, `null` if none): a P3 profile on pixels already converted to sRGB, for instance, which the tags alone can't show. The conversion to sRGB itself is not a change. It is checked whenever either file has a profile, `--metadata=strip` included, unless the output shows another picture than the original's. In human mode, their tags are printed on stderr. Known case: exiftool can't write an ICC profile into an AVIF, so a P3 AVIF whose colours Tinify left in P3 loses it (an unexpected loss, with `pixels:colors`); once converted to sRGB, it needs none.
- **`added`** (`tag`, `after`): tags that appear after the compression, for information.
- Binary values are compared on exiftool's summary (`(Binary data N bytes…)`).

## Motion photos

A motion photo carries a short video after its image. Tinify only returns the image, so `compress-image` detects a motion photo **before** calling Tinify and refuses it (`motion_photo`, exit 20) without using a credit. `details.signals` lists every signal found; one is enough to refuse.

`--allow-motion-photo-loss` compresses it anyway. The XMP declaring the video (`XMP-GCamera:MotionPhoto*`, `XMP-GCamera:MicroVideo*`, `Container:Directory`) is then not copied back, so the compressed file no longer claims a video it doesn't have; the rest of the XMP is kept. These tags, the video itself (`Google:MotionPhotoVideo`, `Samsung:EmbeddedVideo*`) and the XMP toolkit when nothing else remained in the XMP (copied back otherwise) are expected losses, with the reason « photo animée : perte autorisée ».

The detector (`utils/motion_photo.py`, shared with [`inspect`](inspect.md)) looks for these signals, in this order:

| Signal | Found when |
|---|---|
| `xmp_google_motion_photo` | XMP `GCamera:MotionPhoto` = 1 |
| `xmp_google_micro_video` | XMP `GCamera:MicroVideo` = 1 (older Google format) |
| `xmp_container_video` | a `video/*` item in the XMP `Container:Directory` |
| `samsung_motion_photo_data` | the Samsung end-of-file trailer (`SEFT`) has a `MotionPhoto_Data` entry |
| `trailing_data` | bytes after the end of the image that no known structure explains |

- **End of the image**: the EOI of the main JPEG image, the end of the PNG `IEND` chunk, the RIFF size of a WebP. Other formats (AVIF), and a file whose end can't be found (truncated), are not checked for trailing bytes.
- **Known structures** after the image: the images declared by a JPEG's MPF segment (each must be a JPEG inside the file), the items declared by its XMP `Container:Directory` (the primary image's `Padding` after it, then the other items, `Length` plus `Padding` each, ending the file; a directory that doesn't fit explains nothing), and the entries and index of a readable Samsung `SEFT` zone (read by `utils/seft.py`). A `SEFT` zone without `MotionPhoto_Data`, as on many S23 photos, gives no signal. An unreadable index, or bytes outside the entries it describes, give `trailing_data`.
- Any unexplained byte is refused, even zero padding after the EOI: on 543 real phone photos (92 with a `SEFT` zone), only one WhatsApp JPEG carrying 900 KB of unknown bytes after its image was flagged.

## Damaged files

A file whose format is recognised but that can't be decoded to the end (a truncated JPEG, a file cut by an interrupted synchronisation) is refused **before** calling Tinify, without using a credit: `input_corrupt` (exit 27), `details.detected_format`, the decoder's message in `message`. It is a problem of the file, not of compression-suite. The input is fully decoded by Pillow first (`utils/decoding.py`, shared with [`inspect`](inspect.md) and [`preview`](preview.md)); an image above Pillow's decompression-bomb limit (~179 megapixels) is not decoded, so not checked.

exiftool is not the judge: it still describes a file it refuses, with a warning. When it writes nothing at all, it is exiftool itself that failed: `internal_error` (exit 50), its stderr in `message`.

## I/O design

- Without `--json`, **stdout is reserved for binary image data** (when `--output` is omitted) — all reporting goes to stderr
- Compression stats (size before/after, ratio, API compression count) are always printed to stderr
- Compatible with Unix pipes: `cat input.jpg | compress-image | other-tool`
- The format is detected from the file content, never from its extension
- The output is written atomically: a temporary file in the destination folder, then a rename
- The output is written even when it is larger than the original
- When reading from a file, the output gets the original's atime and mtime, applied after exiftool rewrites it (with or without `--json`, whatever `--metadata`). From stdin, there is no date to carry over
- Every check that can fail without Tinify (pinned exiftool, input, format, size, motion photo, existing output, key) runs before the API call, in this order, so no credit is wasted

## JSON output (`--json`)

With `--json`, stdout only contains one JSON document (`schema_version` is bumped on any incompatible change):

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "compress-image",
  "result": {
    "input": {"path": "photo.jpg", "format": "jpeg", "size_bytes": 2481152, "mtime": "2022-08-09T10:11:12.654321+00:00"},
    "output": {"path": "compressed.jpg", "format": "jpeg", "size_bytes": 812345, "mtime": "2022-08-09T10:11:12.654321+00:00"},
    "motion_photo": {"detected": false, "signals": []},
    "tinify": {"compressions_this_month": 42},
    "metadata": {"important": [], "unexpected_losses": [], "expected_losses": [], "added": [], "before": {}, "after": {}}
  }
}
```

`metadata` is the metadata diff described above. `motion_photo` is the detector's result: `detected` is `true` only when the loss was allowed with `--allow-motion-photo-loss`. `mtime` is ISO 8601 in UTC. `input.path` and `input.mtime` are `null` when reading from stdin; `output.mtime` is then the date the output was written. On failure:

```json
{
  "schema_version": 1,
  "ok": false,
  "command": "compress-image",
  "error": {
    "code": "file_too_large",
    "message": "Input exceeds 15MB hard limit (16,000,000 bytes). Use --disable-hard-limit to override.",
    "details": {"size_bytes": 16000000, "limit_bytes": 15728640}
  }
}
```

## Error codes and exit codes

Exit codes are the same with or without `--json`.

| Error code | Exit | Meaning | `details` |
|---|---|---|---|
| — | 0 | Success | |
| `usage_error` | 2 | Invalid options (unknown flag, bad value, `--json` without `--output`) | |
| `tinify_unavailable` | 10 | Tinify server error or network failure | |
| `api_key_invalid` | 11 | Key rejected, Tinify client error | |
| `quota_exceeded` | 12 | Monthly API quota exceeded | |
| `api_key_missing` | 13 | Empty `--api-key`, or none and no usable `tinify_api_key` in the configuration file (absent, unreadable, invalid TOML, empty) | `config_path` |
| `motion_photo` | 20 | Motion photo detected without `--allow-motion-photo-loss` | `signals` |
| `unsupported_format` | 21 | Detected format is not JPEG, PNG, WebP or AVIF | `detected_format` |
| `file_too_large` | 22 | Input > 15MB without `--disable-hard-limit` | `size_bytes`, `limit_bytes` |
| `input_not_found` | 23 | Input missing or not a file | |
| `output_exists` | 24 | Output exists without `--overwrite`, or is a directory | |
| `metadata_copy_failed` | 25 | exiftool failed to copy metadata back | |
| `input_unreadable` | 26 | Input present but not readable (permissions) | |
| `input_corrupt` | 27 | Input of a supported format that its decoder can't read to the end (truncated, for instance), refused before Tinify | `detected_format` |
| `tool_missing` | 30 | Pinned exiftool not installed, not runnable, or of another version than the manifest's | `tool`, `expected_version`, `install_command` |
| `internal_error` | 50 | Any other error, exiftool failing without describing the file among them | |
| `interrupted` | 130 | Interrupted by user (Ctrl+C) | |

## Composability

`compress-image` can be combined with `reduce-jpeg-size` via pipes:

```bash
# First reduce to 5MB, then compress via API
cat large.jpg | compression_suite reduce-jpeg-size --max-size=4999 \
  | compression_suite compress-image --metadata=strip > final.jpg
```
