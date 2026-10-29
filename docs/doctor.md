# doctor

Show the external tools compression-suite really runs, and whether a Tinify key is configured, **without ever showing the key**.

Only a pinned tool that is not usable makes it fail (`tool_missing`, exit 30): a missing system dependency or a missing key is reported, not an error.

## Usage

```bash
# Readable report on stdout
compression_suite doctor

# Machine-readable output (one JSON document on stdout)
compression_suite doctor --json
```

## Options

| Option | Default | Description |
|---|---|---|
| `--json` | `false` | Write a single JSON document on stdout |

## JSON output (`--json`)

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "doctor",
  "result": {
    "pinned_tools": [
      {
        "name": "exiftool",
        "expected_version": "13.55",
        "found_version": "13.55",
        "path": "/home/me/compression-suite/.tools/exiftool-13.55/exiftool",
        "status": "ok",
        "used_by": ["compress-image", "inspect"]
      }
    ],
    "system_dependencies": [
      {"name": "perl", "found_version": "5.30.0", "path": "/usr/bin/perl", "status": "ok", "used_by": ["compress-image", "inspect"]},
      {"name": "ffmpeg", "found_version": "4.2.7-0ubuntu0.1", "path": "/usr/bin/ffmpeg", "status": "ok", "used_by": ["extract-unique-frames", "reassemble-video"]},
      {"name": "jpegoptim", "found_version": null, "path": null, "status": "missing", "used_by": ["reduce-jpeg-size"]}
    ],
    "tinify_api_key": {
      "configured": true,
      "source": "config_file",
      "config_path": "/home/me/.config/compression-suite/config.toml",
      "permissions_ok": true
    }
  }
}
```

- **`pinned_tools`**: every tool of [`pinned-tools.json`](../pinned-tools.json), looked up only in the project's tools folder, never in `PATH`. `path` is where the tool is expected, even when it is missing. `status` is `ok`, `missing` (not installed, or it can't run or its version can't be read: `found_version` is then `null`) or `version_mismatch`.
- **`system_dependencies`**: the programs still taken from the system's `PATH`: Perl (runs the pinned exiftool), ffmpeg (until it is pinned with the video work) and jpegoptim. `status` is `ok` or `missing`; `found_version` is `null` when the program is missing or its version can't be read.
- **`used_by`**: the commands relying on the program. Perl lists the commands of exiftool, which it runs.
- **`tinify_api_key`**: the key is looked for only in the configuration file (see [Tinify API key](../README.md#tinify-api-key)); `--api-key` is a per-call option that doctor can't see. `configured` is `true` when the file holds a usable `tinify_api_key`, and `source` is then `config_file`, else `none`. `permissions_ok` is `false` when the group or the others can read the file, `null` when the file does not exist or can't be reached (a folder on its way that you can't cross). It only looks at reading: a file that others can write but not read is `true`. The key itself never appears in the report.

When a pinned tool is not `ok`, the document is a failure: `error.code` is `tool_missing` (exit 30), and `error.details` holds the same report as `result`. A caller showing the report reads it from `result` when `ok` is `true`, else from `error.details`. Like every `tool_missing` error, the details also give `tool`, `expected_version` (of the first failing tool) and `install_command`:

```json
{
  "schema_version": 1,
  "ok": false,
  "command": "doctor",
  "error": {
    "code": "tool_missing",
    "message": "Pinned tools not usable: exiftool (missing). Install them with: just install-tools",
    "details": {"tool": "exiftool", "expected_version": "13.55", "install_command": "just install-tools", "pinned_tools": ["..."], "system_dependencies": ["..."], "tinify_api_key": {"...": "..."}}
  }
}
```

Without `--json`, the same report is printed on stdout for a human, and the error, if any, on stderr.
