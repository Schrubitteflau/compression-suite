# Compression Suite

Automatically chooses the best compression and processing pipeline based on the content: video, audio, slides, or images. No expertise required!

-   Free software: MIT License

The main idea is to build a content-aware multimedia compression and processing toolbox, fully configurable through human-readable workflows and pipelines defined in formats like YAML.

# Getting started

## Prerequisites

1. **Python 3.10+**: Download from [python.org](https://www.python.org/) or let `uv` install it for you
2. **uv**: A fast Python package manager - [installation guide](https://docs.astral.sh/uv/guides/install-python/)
3. **FFmpeg**: Required for video/audio processing - [download FFmpeg](https://ffmpeg.org/download.html)
    - Linux: `sudo apt install ffmpeg` (Ubuntu/Debian) or `sudo dnf install ffmpeg` (Fedora)
    - macOS: `brew install ffmpeg`
    - Windows: Download from the official website or use `winget install ffmpeg`
4. **Perl**: Required to run the pinned exiftool (`compress-image`, `inspect`) - preinstalled on most Linux and macOS systems
    - Linux: `sudo apt install perl` (Ubuntu/Debian) or `sudo dnf install perl` (Fedora)
5. **just**: Runs the project recipes, including the pinned tools install - [installation guide](https://just.systems/man/en/packages.html)

## Installation

1. Clone this repository:

```bash
git clone https://github.com/yourusername/compression-suite.git
cd compression-suite
```

2. Install dependencies with uv:

```bash
uv sync
```

3. Install the pinned tools:

```bash
just install-tools
```

Some external tools are pinned: instead of the system's, compression-suite uses a copy downloaded into the project (`.tools/`, ignored by git) at the exact version of [`pinned-tools.json`](pinned-tools.json). The recipe downloads each archive from a stable, versioned URL, checks its SHA-256 and extracts it. It can be run again at any time: an already installed tool is kept. Today, only exiftool is pinned (used by `compress-image` and `inspect`); a missing pinned tool makes the command fail with `tool_missing` (exit code 30).

4. Configure the Tinify API key (for `compress-image` only). Get one at https://tinypng.com/developers, then write it in compression-suite's configuration file, readable by you alone (see [Tinify API key](#tinify-api-key)):

```bash
mkdir -p ~/.config/compression-suite
install -m 600 /dev/null ~/.config/compression-suite/config.toml
echo 'tinify_api_key = "YOUR_KEY"' >> ~/.config/compression-suite/config.toml
```

5. Verify the installation:

```bash
uv run compression_suite --help
```

You should see the available commands listed. For more detailed examples, see the [Current usage examples](#current-usage-examples) section below.

### Tinify API key

`compress-image` uses `--api-key` if given, else `tinify_api_key` in `~/.config/compression-suite/config.toml` (`$XDG_CONFIG_HOME/compression-suite/config.toml` when that variable is set). No environment variable is read, and the key never appears in any output. Without a key, the command fails with `api_key_missing` (exit code 13), naming the expected file. If the file is readable by the group or the others, a warning is printed on stderr: run `chmod 600` on it.

# Core concepts

There are two core concepts:

-   Workflows, which describe when and in what order different modules should be executed
-   Modules, which are small units of logic with a single purpose:
    -   detect the type of content
    -   extract images from a video,
    -   find the best lossy compression settings for an image while ensuring that no visible differences remain

Each module can rely on external tools or existing software instead of reinventing the wheel. The goal is to abstract away the manual chaining of commands that users would normally do by hand:

-   `ffmpeg` (video/audio processing)
-   `imagemagick` (image conversion)
-   `sox` / `pydub` (audio)
-   custom scripts

Not clear? Let's see an example.

# Slides presentation

Imagine you've attended an online presentation on a topic that you're really interested in. After the session, you receive a replay/download link so you can watch it again later, in case you missed something. Actually the presentation was sooo interesting that you plan on keeping the file locally. Great, until you realize the video file is 3 GB for only 1 hour and a half. When you think about it, the file mostly consists of a few dozen static images (the slides), and the audio track. So why store a massive video when the content could be represented much more efficiently?

A possible workflow for this use case might look like this.

## Video track pipeline:

1. Extract every distinct frame, optionally cropping parts of the image depending on how the video was recorded (for example, removing the UI of the video-conferencing tool)

2. For each extracted frame, run a module that removes the laser pointer/mouse cursor (using AI, of course!!), but stores its position separately so it can be reconstructed later if needed

3. Detect the webcam area showing the presenter (if not already cropped out). Freeze that section so it becomes a static image across the entire presentation

4. Identify which frames are actually unique slides and annotate them with their timestamps

5. Optionally compress the resulting images for even more gain

6. Rebuild the video track using only the unique slides and the recorded timestamps, drastically reducing the number of frames and therefore the file size :D

### Room for optimizations

There are many opportunities to make each process more efficient. Take the frame-extraction stage, for example.

If the content type is known to be a slide presentation, it may not be necessary to extract every single frame. Instead of processing each of the 60 frames per second, we could sample every 12th frame (or any other ratio). If the user accepts a small loss of synchronization accuracy, the slide transition might occur within at most 11 frames of the detected moment. For a presenter advancing slides manually, that trade-off can drastically improve performance.

Another idea would be to use a dichotomy strategy to detect slide changes. As long as the presenter does not go backward to a previous slide, this would efficiently pinpoint change moments with very few frame reads. The downside is determining the exact timestamps of each slide, which may make this approach less accurate for reconstruction. However, if the goal is simply to extract all unique slides as images, without caring about the timeline, this method could be extremely fast.

To sum up, the more the workflow understands the expected content, the more opportunities exist to optimize it :D

## And what about the audio track?

Since presentations typically involve speech only, encoding the audio using Opus (around 40 kbps) can produce a huge reduction in size with virtually no audible loss in quality.

All of these steps, and their order, can be described in a workflow.

# Wait, that's ambitous

Build a modular toolbox that is flexible enough to cover the majority of real-world use cases, while also supporting lossless compression workflows and algorithms. Quality verification is central: users should not have to manually check whether compression introduced perceptible degradation. Instead, the system should automatically measure quality and ensure any loss stays within a user-authorized margin.

With this approach, a near-optimal workflow could exist for every use case. With additional engineering, the system could even automatically detect the media type and apply the most appropriate workflow without human intervention.

## Features

As of now, this project is more an idea than anything else. No workflows, no smart adaptation based on the file content, no parameterization, only a single script that currently aims to perform that Slides presentation example, in a simpler way: extract frames, deduplicate, rebuild the video track, and compress the audio in Opus.

Voilà 😉

# Current usage examples

## Complete workflow for a presentation video

This is ideal for large presentation videos with only a few dozen unique slides but bloated file sizes from constant-framerate recording or inefficient video containers. For long-term storage/archival, you can use either a lightweight reconstructed video file (in VFR mode) or the folder containing the extracted frames, timeline, and audio.

```bash
# Extract the unique frames to a folder
uv run compression_suite extract-unique-frames mypresentation.mp4 ./mypresentation-lightweight --output-format multiframe-webp

# Extract the audio track - opus format with 32k bitrate is chosen here because it's a great compromise between size and quality for human voices
ffmpeg -i mypresentation.mp4 -map a:0 -c:a libopus -b:a 32k ./mypresentation-lightweight/audio.opus

# You can reassemble the video from the extracted frames with the audio track when you want to watch it again

# Option 1: VFR mode (variable framerate) - most lightweight, timing-perfect reconstruction
# Stores only unique frames with exact timing. Best for archival and minimal file size.
uv run compression_suite reassemble-video ./mypresentation-lightweight mypresentation-reconstructed-vfr.mp4 --audio ./mypresentation-lightweight/audio.opus --mode vfr

# Option 2: CFR mode (constant framerate) - better player compatibility
# Duplicates frames to maintain constant framerate. Larger file size, but plays smoothly in VLC and most players.
uv run compression_suite reassemble-video ./mypresentation-lightweight mypresentation-reconstructed-cfr.mp4 --audio ./mypresentation-lightweight/audio.opus --mode cfr --fps 25
```

## Additional options

```bash
# Extract frames as individual PNGs instead of a single WebP file
uv run compression_suite extract-unique-frames video.mp4 ./frames --output-format png

# Ignore a fixed source-pixel rectangle while comparing frames. The rectangle
# remains visible in saved frames, but motion inside it does not create a new frame.
uv run compression_suite extract-unique-frames video.mp4 ./frames --ignore-region 0,0,480,720

# Repeat the flag to ignore multiple rectangles.
uv run compression_suite extract-unique-frames video.mp4 ./frames \
  --ignore-region 0,0,480,720 \
  --ignore-region 1100,0,180,100

# Interactively draw ignored rectangles with OpenCV on the video's midpoint frame.
# Install the optional GUI dependency first: uv sync --extra gui
uv run compression_suite extract-unique-frames video.mp4 ./frames --manual-select-ignore-region

# Display a specific frame in the interactive selector instead.
uv run compression_suite extract-unique-frames video.mp4 ./frames \
  --manual-select-ignore-region \
  --selection-timestamp 42.5

# Reassemble with custom encoding settings and CFR mode at 30 fps
uv run compression_suite reassemble-video ./frames output.mp4 --crf 20 --preset slow --mode cfr --fps 30

# Reassemble with VFR mode (default) - most compact file
uv run compression_suite reassemble-video ./frames output.mp4

# View all options
uv run compression_suite extract-unique-frames --help
uv run compression_suite reassemble-video --help
```

Comparison exclusions use `X,Y,WIDTH,HEIGHT` coordinates measured from the
source video's top-left corner. Explicit and manual selection cannot be used
together. The extracted image remains complete, so an excluded presenter or
overlay appears frozen between retained presentation changes. New metadata uses
version 1.1 and records the resolved rectangles as
`comparison_exclusion_regions`.

During manual selection, draw one rectangle and press Space or Enter to accept
it. The selector closes immediately and the terminal prints the accumulated
`--ignore-region` flags. Choose whether to add another rectangle, then confirm
the complete list. Answer `n` at the final confirmation to start the selection
again, or accept the coordinates to begin extraction. Rectangle edges within
10 source pixels of a video boundary snap exactly to that boundary.

## Technical Notes

### Performance Optimizations

**CFR Mode Symlinks**: To maximize performance, CFR mode uses symlinks for duplicate frames rather than writing the same image multiple times to disk. Each unique frame is written once, and symlinks are created for all duplicates. This dramatically reduces I/O overhead and makes CFR mode nearly as fast as VFR mode.

**Frame Piping**: Direct frame piping to FFmpeg via stdin is not currently implemented for video reconstruction. The current approach uses temporary files (with symlinks for efficiency in CFR mode) which provides a good balance of simplicity, reliability, and performance.

## Known Limitations

### VFR Mode: Last Frame May Be Missing

There is a known issue with VFR (Variable Frame Rate) mode where the final slide may not appear in the reconstructed video. The video duration remains correct, but FFmpeg's concat demuxer appears to drop the last frame during encoding despite the concat file being properly formatted.

**Workaround**: Use CFR (Constant Frame Rate) mode instead, which reliably includes all frames:
```bash
uv run compression_suite reassemble-video ./frames output.mp4 --mode cfr --fps 25
```

CFR mode duplicates frames to maintain a constant framerate, resulting in a larger file size but ensuring all slides are present, and with a better player compatibility.

## Credits

This package was created with [Cookiecutter](https://github.com/audreyfeldroy/cookiecutter) and the [audreyfeldroy/cookiecutter-pypackage](https://github.com/audreyfeldroy/cookiecutter-pypackage) project template.

[ffmpeg](https://www.ffmpeg.org/) is fantastic.
