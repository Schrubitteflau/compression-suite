"""How every video command is stopped: SIGINT, SIGTERM and SIGHUP become an interruption (`interrupted`, 130) from the
moment the command starts its checks (its CLI function enters `Interruptions`) until the output takes its name; after
that, they are ignored until the JSON document is written.

A signal while the process is still starting (Python loading the command, before the handlers are installed) ends
it by that signal (exit 128 + its number), with no JSON on stdout and nothing written: accepted, the window is short
and the caller sees the process killed.

A command wraps its whole run, the writing of the envelope included, in `Interruptions`, and hands `hold` to the
shared output writer (`utils.ffmpeg_output`), which calls it right before the output takes its name.
"""

import signal
from typing import Any


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP received: handled as an interruption, so that every cleanup runs."""


class Interruptions:
    """SIGINT, SIGTERM and SIGHUP as interruptions, for the whole command, until the output is in place.

    A SIGHUP already ignored (under `nohup`) stays ignored. Only the first signal interrupts: the next ones are
    ignored, so that the cleanup it starts runs to the end. From `hold()`, called right before the output takes its
    name, the signals are ignored: the command is then done and reported as such, never an `interrupted` envelope
    with a complete output on disk. The block must hold the writing of the envelope.

    With `until_exit`, the signals are ignored once the block is over, until the process exits: one arriving after
    the envelope (the interpreter shutting down) leaves the exit code of the envelope, 0 for a done command, 130 for
    an interrupted one, rather than killing the process. A Python handler wouldn't do: the interpreter puts the
    default one back for SIGINT while shutting down.
    """

    def __init__(self, until_exit: bool) -> None:
        self.until_exit = until_exit
        self.held = False
        self.previous: dict[int, Any] = {}

    def __enter__(self) -> "Interruptions":
        signals = [signal.SIGINT, signal.SIGTERM]
        if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
            signals.append(signal.SIGHUP)
        for signum in signals:
            self.previous[signum] = signal.signal(signum, self._interrupt)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, signal.SIG_IGN if self.until_exit else handler)

    def hold(self) -> None:
        """Ignore the signals from now on: the output is taking its name, the command can no longer be undone."""
        self.held = True

    def _interrupt(self, signum: int, frame: Any) -> None:
        if self.held:
            return
        self.held = True  # the next signals must not cut the cleanup this one starts
        raise KeyboardInterrupt() if signum == signal.SIGINT else Terminated()


def never_held() -> None:
    """The `hold` of a caller that installs no `Interruptions` (a test calling a command's core directly)."""
