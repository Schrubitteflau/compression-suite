# CLI contract

What a program calling compression-suite can rely on: the commands it runs, the JSON document each one writes under
`--json`, and the exit codes. This page is the entry point; each fact lives in one place and is linked from here, not
restated.

## Where each part lives

| Part | Its one place |
|---|---|
| Envelope, `schema_version`, error codes and their exit codes | [`utils/contract.py`](../src/compression_suite/utils/contract.py): the module docstring, `SCHEMA_VERSION`, `ErrorCode`, `EXIT_CODES` |
| What the video commands share | Reading the video: [`utils/video_probe.py`](../src/compression_suite/utils/video_probe.py); `result` fields: [`utils/video_command.py`](../src/compression_suite/utils/video_command.py); atomic writing: [`utils/ffmpeg_output.py`](../src/compression_suite/utils/ffmpeg_output.py); signals: [`utils/interruptions.py`](../src/compression_suite/utils/interruptions.py); stderr announces: `announce_tool_command` in `contract.py` |
| A command's options | `compression_suite COMMAND --help`, from its `cli.py` |
| A command's `result` fields, and the error codes it gives with their `details` | Its code, linked below, and its tests |
| Tests holding the contract | [`test_video_contract.py`](../src/compression_suite/test_video_contract.py) and each command's tests |

## Rules

- **One document on stdout**: under `--json`, stdout holds exactly one JSON document, the envelope, and nothing else.
  `ok` tells success (`result`) from failure (`error`: `code`, `message`, `details`). Usage errors (bad options) give
  the envelope too, with `usage_error`, on every command listed below.
- **Exit code**: bound to the error code (`EXIT_CODES`), the same with or without `--json`; 0 on success. A failure
  exits non-zero *and* writes its document, so a caller reads the document, not only the code. One exception writes
  no document: a signal while Python is still loading the command (exit 128 + the signal's number).
- **Version**: every document, and every JSON line on stderr, carries `schema_version`, one version for the whole CLI.
  Any incompatible change to a document's shape bumps it; adding a field does not. A caller checks it and refuses a
  version it doesn't know. Current version: 2 (what changed in 2: the docstring of `contract.py`).
- **Error codes**: a new code only when the caller would react differently; otherwise the reason goes in `message`.
  `message` is for humans and may change; `code` and the `details` keys a command gives are stable.
- **stderr**: under `--json`, the machine-readable lines are JSON objects, each with `schema_version` and `command`:
  a `tool_command` announce before each ffmpeg run (video commands), and `progress` lines (`compress-video`). Other
  text may appear there (warnings, a crash's traceback): a caller ignores any line it doesn't know.

## Commands the caller runs

Each runs with `--json`. The options listed are the ones the caller passes; `--help` has them all.

| Command | Options passed | Code |
|---|---|---|
| `inspect FILE` | none | [`inspect_file/`](../src/compression_suite/inspect_file/) |
| `preview FILE` | `-o`, `--overwrite` | [`preview/`](../src/compression_suite/preview/) |
| `compress-image FILE` | `--output`, `--overwrite`, `--metadata keep`, `--allow-motion-photo-loss` | [`compress_image/`](../src/compression_suite/compress_image/) |
| `video-presets` | none | [`utils/video_presets.py`](../src/compression_suite/utils/video_presets.py) |
| `display-copy VIDEO` | `--output`, `--overwrite` | [`display_copy/`](../src/compression_suite/display_copy/) |
| `preview-portion VIDEO` | `--preset`, `--crf`, `--start`, `--duration`, `--output` | [`preview_portion/`](../src/compression_suite/preview_portion/) |
| `compress-video VIDEO` | `--preset`, `--crf`, `--output`; reads the `progress` lines | [`compress_video/`](../src/compression_suite/compress_video/) |
| `doctor` | none | [`doctor/`](../src/compression_suite/doctor/) |

Error codes the caller branches on, beyond showing `message`: `input_not_found`, `input_unreadable`,
`input_corrupt`, `motion_photo`, `tool_missing` (it shows `details.install_command`), `api_key_missing`, and
`usage_error` from `preview-portion` with `details.start` (a start past the video's end). Changing the meaning of
one of these, or of the `details` keys named, is an incompatible change.
