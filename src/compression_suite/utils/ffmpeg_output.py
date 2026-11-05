"""Run the pinned ffmpeg, and write a video command's output so that it appears whole or not at all, even if the
process is killed. Shared by every video command (display-copy, preview-portion, compress-video).

`atomic_output` gives the command an anonymous file (`O_TMPFILE`) of the destination folder to write into, by ffmpeg
(`/dev/fd/<fd>`, inherited) or any other tool (`/proc/<pid>/fd/<fd>`, which another process of the user can open): it
has no name until it is complete, so a run killed halfway, even by SIGKILL, leaves nothing behind, neither a partial
output nor a temporary file. Once the command is done, the file takes the output's name: without `--overwrite`, by a
hard link, which fails if a file appeared at the output meanwhile (`output_exists`, the file kept); with it, under a
short hidden temporary name (`.<8 hex>.tmp`, so that an output name of up to 255 bytes still works) renamed over the
output, atomically. On a file system without `O_TMPFILE`, a hidden named temporary file is used instead, removed if
the command fails or is interrupted, but not after a SIGKILL. The output's missing folders are created, and removed
again on a failure or an interruption (not after a SIGKILL); a new folder that another call removed meanwhile (it
looked empty to that call's cleanup) is made again before naming.

Right before the name is given, the command's `hold` is called (see `utils.interruptions`): from then on, a signal no
longer interrupts it. ffmpeg runs in a session of its own, out of reach of a Ctrl+C sent to the terminal: an
interruption reaches Python, which kills ffmpeg and waits for it. ffmpeg also gets SIGKILL if compression-suite dies
first, so it never keeps encoding on its own.

A command that runs ffmpeg twice (the preview portions) writes the first output into an `anonymous_file`, with
`run_ffmpeg`, and hands it to the second run as `/dev/fd/<fd>` (`pass_fds`): it has no name, so nothing is left after
any interruption.

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
from dataclasses import dataclass
from pathlib import Path

from compression_suite.compress_image.main import check_output, current_umask
from compression_suite.utils.contract import CommandError, ErrorCode, announce_tool_command
from compression_suite.utils.formats import UNKNOWN_FORMAT
from compression_suite.utils.pinned_tools import resolve_tool
from compression_suite.utils.video_probe import corrupt_error

PR_SET_PDEATHSIG = 1
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400

# Errors of a file system without O_TMPFILE
NO_TMPFILE_ERRNOS = (errno.EOPNOTSUPP, errno.EISDIR, errno.EINVAL, errno.ENOENT)

# Attempts at naming an output whose folder a parallel call removes meanwhile (see `_name_anonymous`)
NAMING_ATTEMPTS = 3

# Lines of ffmpeg's error output that say the input is at fault, not the output or the machine: its demuxer, its
# streams and their decoders (each decoder also logs under its own name, see `input_errors`)
INPUT_ERROR_PREFIXES = ("[in#", "[vist#", "[aist#")
INPUT_ERROR_MARKERS = ("[dec:", "Invalid data found", "Error while decoding", "partial file")

# Called with the seconds ffmpeg has written so far
ProgressCallback = Callable[[float], None]


def _die_with(parent: int) -> Callable[[], None]:
    """What the ffmpeg child runs before exec: ask for SIGKILL when the parent dies, and exit if it already died
    between the fork and the request."""

    def setup() -> None:
        with contextlib.suppress(OSError, AttributeError):
            ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent:
            os._exit(1)

    return setup


def _run_ffmpeg(
    ffmpeg: Path, args: list[str], pass_fds: tuple[int, ...], on_progress: ProgressCallback | None
) -> subprocess.CompletedProcess:
    """Run the pinned ffmpeg to its end, or kill it and wait for it if this process is interrupted.

    With `on_progress`, ffmpeg reports its progress on stdout (`-progress pipe:1`), passed on as it comes.
    """
    progress = ["-nostats", "-progress", "pipe:1"] if on_progress is not None else []
    command = [str(ffmpeg), "-v", "error", "-nostdin", "-y", *progress, *args]
    announce_tool_command(command)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if on_progress is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=pass_fds,
        start_new_session=True,
        preexec_fn=_die_with(os.getpid()),
    )
    errors: list[str] = []
    reader = threading.Thread(target=lambda: errors.extend(process.stderr or []), daemon=True)
    reader.start()
    try:
        for line in process.stdout or []:
            key, _, value = line.strip().partition("=")
            if on_progress is not None and key == "out_time_us" and value.lstrip("-").isdigit():
                on_progress(max(0, int(value)) / 1_000_000)
        process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        reader.join()
    return subprocess.CompletedProcess(command, process.returncode, "", "".join(errors))


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


def ffmpeg_failure(
    completed: subprocess.CompletedProcess, decoders: tuple[str, ...], detected_format: str = UNKNOWN_FORMAT
) -> CommandError | None:
    """None if ffmpeg succeeded without an input error; `input_corrupt` if any of its errors points at the input,
    even concealed (`details.detected_format` being the input's container); else `internal_error` (disk full, for
    instance)."""
    errors = input_errors(completed.stderr, decoders)
    if completed.returncode == 0 and not errors:
        return None
    lines = errors or [line.strip() for line in completed.stderr.splitlines() if line.strip()]
    reason = " / ".join(lines[-3:]) or f"ffmpeg exited with code {completed.returncode}"
    if errors:
        return corrupt_error(detected_format, reason)
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


def _appeared(output_path: Path) -> CommandError:
    return CommandError(
        ErrorCode.OUTPUT_EXISTS,
        f"Output file appeared while the command ran: {output_path}. Use --overwrite to replace.",
    )


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


@dataclass(frozen=True)
class OutputFile:
    """The file a command writes its output into, before it takes the output's name."""

    fd: int

    @property
    def ffmpeg_path(self) -> str:
        """The path ffmpeg writes to or reads from, the fd being in its `pass_fds`."""
        return f"/dev/fd/{self.fd}"

    @property
    def path(self) -> Path:
        """A path this process and any other tool (exiftool) can open, without inheriting the fd."""
        return Path(f"/proc/{os.getpid()}/fd/{self.fd}")


@contextlib.contextmanager
def atomic_output(output_path: Path, overwrite: bool, hold: Callable[[], None]) -> Iterator[OutputFile]:
    """A file to write the output into, named `output_path` once the block ends without an error (see the module).

    Raises:
        CommandError: `output_exists` if a file appeared at the output while the block ran, without `overwrite`.
    """
    created = _make_folders(output_path.parent)
    tmp_path = None
    try:
        fd = _open_anonymous(output_path.parent)
        if fd is None:
            fd, name = tempfile.mkstemp(dir=output_path.parent, prefix=".", suffix=".tmp")
            tmp_path = Path(name)
            os.fchmod(fd, 0o666 & ~current_umask())
        try:
            yield OutputFile(fd)
            hold()
            if tmp_path is None:
                _name_anonymous(fd, output_path, overwrite, created)
            else:
                _name_temporary(tmp_path, output_path, overwrite)
        finally:
            os.close(fd)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        for folder in reversed(created):  # the deepest first; only if empty
            with contextlib.suppress(OSError):
                folder.rmdir()
        raise


def _name_anonymous(fd: int, output_path: Path, overwrite: bool, created: list[Path]) -> None:
    """Give a complete anonymous file the output's name: a link that fails if the output exists, or with
    `overwrite`, a link under a hidden temporary name renamed over the output.

    Another call writing into the same new folder may have removed it meanwhile, as it cleaned up after an
    interruption (the folder looked empty: an anonymous file doesn't show): the folders are made again, added to
    `created`, and the link tried again, a few times.
    """
    for attempt in range(NAMING_ATTEMPTS):
        try:
            _link_once(fd, output_path, overwrite)
            return
        except FileNotFoundError:
            if attempt == NAMING_ATTEMPTS - 1 or output_path.parent.exists():
                raise
            created += [folder for folder in _make_folders(output_path.parent) if folder not in created]


def _link_once(fd: int, output_path: Path, overwrite: bool) -> None:
    """One attempt of `_name_anonymous`."""
    if not overwrite:
        try:
            _link_following(f"/proc/self/fd/{fd}", output_path)
        except FileExistsError:
            raise _appeared(output_path) from None
        return
    tmp_path = output_path.with_name(f".{os.urandom(4).hex()}.tmp")
    _link_following(f"/proc/self/fd/{fd}", tmp_path)
    try:
        os.replace(tmp_path, output_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _name_temporary(tmp_path: Path, output_path: Path, overwrite: bool) -> None:
    """Give a complete named temporary file the output's name: a hard link that fails if the output exists (on a file
    system without hard links, the output is checked once more right before the rename), or with `overwrite`, a
    rename over the output."""
    if not overwrite:
        try:
            os.link(tmp_path, output_path)
        except FileExistsError:
            raise _appeared(output_path) from None
        except OSError:
            check_output(output_path, overwrite=False)
        else:
            tmp_path.unlink()
            return
    os.replace(tmp_path, output_path)


@contextlib.contextmanager
def anonymous_file(folder: Path | None = None) -> Iterator[int]:
    """A scratch file of `folder` (the temporary folder by default), readable and writable, that has no name: it
    vanishes once closed, even if the process is killed. ffmpeg reaches it as `/dev/fd/<fd>`, given in `pass_fds`,
    and another tool as `/proc/<pid>/fd/<fd>`.

    On a file system without `O_TMPFILE`, a named file is created and unlinked at once.
    """
    folder = folder or Path(tempfile.gettempdir())
    fd = _open_anonymous(folder)
    if fd is None:
        fd, name = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
        os.unlink(name)
    try:
        yield fd
    finally:
        os.close(fd)


def run_ffmpeg(
    args: list[str],
    decoders: tuple[str, ...] = (),
    pass_fds: tuple[int, ...] = (),
    detected_format: str = UNKNOWN_FORMAT,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Run the pinned ffmpeg to its end, killed if this process is interrupted.

    `decoders` names the decoders of the input that ffmpeg runs (`hevc`, `pcm_s16le`...): an error logged by one of
    them makes the input damaged, `detected_format` being its container. `pass_fds` are file descriptors ffmpeg
    inherits, to read or write an `anonymous_file` or an `OutputFile` as `/dev/fd/<fd>`.

    Raises:
        CommandError: `input_corrupt` or `internal_error` if ffmpeg fails, `tool_missing` if it is not usable.
        KeyboardInterrupt: on an interruption, once ffmpeg is killed.
    """
    ffmpeg = resolve_tool("ffmpeg")
    failure = ffmpeg_failure(_run_ffmpeg(ffmpeg, args, pass_fds, on_progress), decoders, detected_format)
    if failure is not None:
        raise failure


def write_with_ffmpeg(
    output_path: Path,
    build_args: Callable[[str], list[str]],
    overwrite: bool,
    hold: Callable[[], None],
    decoders: tuple[str, ...] = (),
    pass_fds: tuple[int, ...] = (),
    detected_format: str = UNKNOWN_FORMAT,
) -> None:
    """Run the pinned ffmpeg to write `output_path` atomically, through `atomic_output`.

    `build_args` receives the path ffmpeg must write to and returns ffmpeg's arguments (inputs, options, and that
    path, with `-f` set: the path has no extension; outputs that write nothing, `-f null -`, may follow). See
    `run_ffmpeg` for the others.

    Raises:
        CommandError: `input_corrupt` or `internal_error` if ffmpeg fails, `tool_missing` if it is not usable,
            `output_exists` if a file appeared at the output meanwhile, without `overwrite`.
        KeyboardInterrupt: on an interruption, once ffmpeg is killed and nothing is left.
    """
    resolve_tool("ffmpeg")  # `tool_missing` before anything is created
    with atomic_output(output_path, overwrite, hold) as output:
        run_ffmpeg(build_args(output.ffmpeg_path), decoders, (output.fd, *pass_fds), detected_format)
