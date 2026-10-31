"""How a video stream is rendered for display in a browser: H.264 8 bits 4:2:0, visually lossless, SDR.

Shared by every command that hands a video to WelcomeApp's players (the display copy, the preview portions, the
copy of an export), so that they all look alike.

- libx264, CRF 16, preset `superfast`: visually lossless, and fast enough not to keep the user waiting.
- Every frame keeps its exact timestamp: `-fps_mode passthrough` keeps every frame, and `-enc_time_base:v demux`
  keeps the input's clock (a phone's 90 kHz), instead of snapping each time to a multiple of 1/30 s.
- The rotation is applied to the pixels (ffmpeg's autorotate, which then writes no display matrix).
- HDR (PQ or HLG) is tone-mapped to SDR BT.709: brought down to 8 bits as is, its colours would look washed out.
  An SDR 10-bit video only goes to 8 bits.
"""

from compression_suite.utils.video_probe import VideoProbe

X264_ARGS = ("-c:v", "libx264", "-preset", "superfast", "-crf", "16", "-pix_fmt", "yuv420p")
TIMESTAMP_ARGS = ("-fps_mode", "passthrough", "-enc_time_base:v", "demux")

# Nominal peak luminance (cd/m²) mapped to SDR white by the tone mapping
HDR_NOMINAL_PEAK = 100
SDR_TAGS = ("-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv")


# Colour descriptions that say nothing: the HDR defaults (BT.2020) apply
UNSPECIFIED = (None, "", "unknown", "unspecified", "reserved")
# zscale has no constant-luminance BT.2020 matrix: the non-constant one is the closest
ZSCALE_MATRICES = {"bt2020c": "bt2020nc"}


def tone_mapping_filter(probe: VideoProbe) -> str:
    """The zscale and tonemap chain from the video's HDR transfer to SDR BT.709 8 bits.

    Its input colours are named explicitly, BT.2020 when the video doesn't say (zscale can't always read them from
    the frames, HLG in particular, nor from a container's `colr` box alone), and the first step's output colours
    too: without them, zscale finds no path between colour spaces.
    """
    video = probe.video
    primaries = video.get("color_primaries")
    primaries = "bt2020" if primaries in UNSPECIFIED else primaries
    matrix = video.get("color_space")
    matrix = "bt2020nc" if matrix in UNSPECIFIED else ZSCALE_MATRICES.get(matrix, matrix)
    return ",".join(
        [
            f"zscale=tin={probe.hdr_transfer}:pin={primaries}:min={matrix}"
            f":t=linear:npl={HDR_NOMINAL_PEAK}:p=bt2020:m=bt2020nc",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ]
    )


def display_video_args(probe: VideoProbe) -> list[str]:
    """ffmpeg's output arguments for the video stream rendered for display (the stream itself is mapped by the
    caller)."""
    args = [*X264_ARGS, *TIMESTAMP_ARGS]
    if probe.hdr_transfer is not None:
        args += ["-vf", tone_mapping_filter(probe), *SDR_TAGS]
    return args
