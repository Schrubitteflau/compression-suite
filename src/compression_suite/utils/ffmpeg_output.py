"""Run the pinned ffmpeg into an output file that appears whole or not at all, even if the process is killed.

ffmpeg writes into an anonymous file (`O_TMPFILE`) of the destination folder: it has no name until it is complete,
so a run killed halfway, even by SIGKILL, leaves nothing behind, neither a partial output nor a temporary file. Once
ffmpeg succeeds, the file gets a short hidden temporary name (`.<8 hex>.tmp`, so that an output name of up to 255
bytes still works) and is renamed over the output, atomically. On a file system without `O_TMPFILE`, a hidden named
temporary file is used instead, removed if the run fails or is interrupted, but not after a SIGKILL.

From the start of ffmpeg to the final rename, SIGTERM and SIGHUP are turned into `KeyboardInterrupt`, like SIGINT
(a SIGHUP already ignored, under `nohup`, stays ignored): ffmpeg is then killed and waited for, nothing is left, and
the command reports `interrupted`. ffmpeg also gets SIGKILL if compression-suite dies first, so it never keeps
encoding on its own.

Any error of the input's demuxer or decoders makes the input damaged (`input_corrupt`), even when ffmpeg conceals it
and succeeds: ffmpeg is pinned, so a decoder error means a damaged file, as for the images.
"""

import contextlib
import ctypes
import errno
import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from compression_suite.compress_image.main import current_umask
from compression_suite.utils.contract import CommandError, ErrorCode
from compression_suite.utils.pinned_tools import resolve_tool

PR_SET_PDEATHSIG = 1
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400

# Errors of a file system without O_TMPFILE
NO_TMPFILE_ERRNOS = (errno.EOPNOTSUPP, errno.EISDIR, errno.EINVAL, errno.ENOENT)

# Lines of ffmpeg's error output that say the input is at fault, not the output or the machine: its demuxer, its
# streams and their decoders (each decoder also logs under its own name, see `input_errors`)
INPUT_ERROR_PREFIXES = ("[in#", "[vist#", "[aist#")
INPUT_ERROR_MARKERS = ("[dec:", "Invalid data found", "Error while decoding", "partial file")


def _die_with(parent: int) -> Callable[[], None]:
    """What the ffmpeg child runs before exec: ask for SIGKILL when the parent dies, and exit if it already died
    between the fork and the request."""

    def setup() -> None:
        with contextlib.suppress(OSError, AttributeError):
            ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent:
            os._exit(1)

    return setup


@contextlib.contextmanager
def _signals_interrupt() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into KeyboardInterrupt, from the main thread only (where Python handles signals)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signals = [signal.SIGTERM]
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
        signals.append(signal.SIGHUP)
    previous = {sig: signal.signal(sig, interrupt) for sig in signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _run_ffmpeg(ffmpeg: Path, args: list[str], pass_fds: tuple[int, ...]) -> subprocess.CompletedProcess:
    """Run the pinned ffmpeg to its end, or kill it and wait for it if this process is interrupted."""
    command = [str(ffmpeg), "-v", "error", "-nostdin", "-y", *args]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=pass_fds,
        preexec_fn=_die_with(os.getpid()),
    )
    try:
        _, stderr = process.communicate()
    except BaseException:
        process.kill()
        process.wait()
        raise
    return subprocess.CompletedProcess(command, process.returncode, "", stderr)


def input_errors(stderr: str, decoders: tuple[str, ...]) -> list[str]:
    """The lines of ffmpeg's error output that come from the input: its demuxer, its streams, its decoders (logged
    under their names, `[hevc @ 0x...]`)."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return [
        line
        for line in lines
        if line.startswith(INPUT_ERROR_PREFIXES)
        or any(marker in line for marker in INPUT_ERROR_MARKERS)
        or any(line.startswith(f"[{decoder} @") for decoder in decoders)
    ]


def ffmpeg_failure(completed: subprocess.CompletedProcess, decoders: tuple[str, ...]) -> CommandError | None:
    """None if ffmpeg succeeded without an input error; `input_corrupt` if any of its errors points at the input,
    even concealed; else `internal_error` (disk full, for instance)."""
    errors = input_errors(completed.stderr, decoders)
    if completed.returncode == 0 and not errors:
        return None
    lines = errors or [line.strip() for line in completed.stderr.splitlines() if line.strip()]
    reason = " / ".join(lines[-3:]) or f"ffmpeg exited with code {completed.returncode}"
    if errors:
        return CommandError(ErrorCode.INPUT_CORRUPT, f"The video can't be decoded, the file is damaged: {reason}")
    return CommandError(ErrorCode.INTERNAL_ERROR, f"ffmpeg failed: {reason}")


def _open_anonymous(folder: Path) -> int | None:
    """An anonymous file in `folder`, readable and writable, or None if the file system can't make one."""
    try:
        return os.open(folder, os.O_TMPFILE | os.O_RDWR, 0o666)  # the umask applies
    except OSError as e:
        if e.errno in NO_TMPFILE_ERRNOS:
            return None
        raise


def _link_following(source: str, destination: Path) -> None:
    """`linkat` with `AT_SYMLINK_FOLLOW`, the only unprivileged way to name an anonymous file: up to Python 3.13,
    `os.link` calls `link`, which links the `/proc/self/fd` symlink itself and fails with EXDEV."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.linkat(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), AT_SYMLINK_FOLLOW) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), source, None, str(destination))


def _give_name(fd: int, output_path: Path) -> None:
    """Link a complete anonymous file into its folder under a hidden temporary name, then rename it over the
    output."""
    tmp_path = output_path.with_name(f".{os.urandom(4).hex()}.tmp")
    _link_following(f"/proc/self/fd/{fd}", tmp_path)
    try:
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_with_ffmpeg(
    output_path: Path, build_args: Callable[[str], list[str]], decoders: tuple[str, ...] = ()
) -> None:
    """Run the pinned ffmpeg to write `output_path` atomically.

    The output's missing folders are created, and removed again if the copy fails or is interrupted (not after a
    SIGKILL, when nothing runs).

    `build_args` receives the path ffmpeg must write to and returns ffmpeg's arguments (inputs, options, and that
    path, with `-f` set: the path has no extension; outputs that write nothing, `-f null -`, may follow).
    `decoders` names the decoders of the input that ffmpeg runs (`hevc`, `pcm_s16le`...): an error logged by one of
    them makes the input damaged.

    Raises:
        CommandError: `input_corrupt` or `internal_error` if ffmpeg fails, `tool_missing` if it is not usable.
        KeyboardInterrupt: on SIGINT, SIGTERM or SIGHUP, once ffmpeg is killed and nothing is left.
    """
    ffmpeg = resolve_tool("ffmpeg")  # `tool_missing` before anything is created
    created = _make_folders(output_path.parent)
    try:
        with _signals_interrupt():
            _write(ffmpeg, output_path, build_args, decoders)
    except BaseException:
        for folder in reversed(created):  # the deepest first; only if empty
            with contextlib.suppress(OSError):
                folder.rmdir()
        raise


def _make_folders(folder: Path) -> list[Path]:
    """Create a folder and its missing parents; return those created, the outermost first."""
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    missing.reverse()
    for path in missing:
        path.mkdir(exist_ok=True)
    return missing


def _write(ffmpeg: Path, output_path: Path, build_args: Callable[[str], list[str]], decoders: tuple[str, ...]) -> None:
    """The run and the rename, under the signal handlers, into the output's existing folder."""
    fd = _open_anonymous(output_path.parent)
    if fd is not None:
        try:
            failure = ffmpeg_failure(_run_ffmpeg(ffmpeg, build_args(f"/dev/fd/{fd}"), (fd,)), decoders)
            if failure is not None:
                raise failure
            _give_name(fd, output_path)
        finally:
            os.close(fd)
        return

    fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=".", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.chmod(0o666 & ~current_umask())
        failure = ffmpeg_failure(_run_ffmpeg(ffmpeg, build_args(str(tmp_path)), ()), decoders)
        if failure is not None:
            raise failure
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
