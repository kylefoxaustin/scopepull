"""Where a pull narrates itself.

The CLI and the window show the same lines, and the window shows them by
reading the CLI's stdout, so these lines carry no rich markup: whatever the owl
says has to survive a pipe, a .log file and a Treeview unchanged. `raw` is the
escape hatch for in-place progress (a line ending in `\\r`), which belongs on a
terminal but must never be written to a log file one carriage return at a time.
"""

from __future__ import annotations

from collections.abc import Callable


class Log:
    """A line sink with an optional raw channel.

    `log("...")` is one finished line. `log.raw("... \\r")` is progress that
    overwrites itself and is dropped entirely by sinks that can't show it
    (a file, a list in a test).
    """

    def __init__(
        self, write: Callable[[str], None], raw: Callable[[str], None] | None = None
    ) -> None:
        self._write = write
        self._raw = raw

    def __call__(self, text: str = "") -> None:
        self._write(text)

    def raw(self, text: str) -> None:
        if self._raw is not None:
            self._raw(text)


def quiet() -> Log:
    """A log that says nothing (for callers that only want the return value)."""
    return Log(lambda _text: None)
