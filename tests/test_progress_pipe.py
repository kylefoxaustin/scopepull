"""Progress lines have to survive a pipe.

The window shows a pull by reading the CLI's stdout, and it redraws the current
line whenever a segment ends in `\\r`. So the `\\r` has to come out of the pipe
verbatim — not folded into `\\n`, not word-wrapped by rich, not buffered until
the end. starstack learned this the hard way in its 0.2.5-0.2.8 run; the test
is here now because this side is the one that writes the lines.
"""

from __future__ import annotations

import os
import subprocess
import sys

CR, NL = chr(13), chr(10)

SNIPPET = """
import sys
from scopepull.cli import _console_log

log = _console_log()
log("Pulling 1 observation(s) to /tmp/x")
log.raw("  [1/1] M31: building 3/10 frames\\r")
log.raw("  [1/1] M31: building 10/10 frames\\r")
log.raw("\\n")
log("  [1/1] M31: ingested 10 frames, 10 FITS")
log("  into " + "C:/Users/someone/A very long path that must not be folded " * 2)
"""


def test_progress_survives_a_pipe():
    # Bytes, not text=True: universal newlines fold every carriage return
    # into a newline, and the test would pass while the window stayed
    # broken. That is why gui._relay reads the child a byte at a time.
    # why gui._relay reads the child a byte at a time.
    r = subprocess.run(
        [sys.executable, "-c", SNIPPET],
        capture_output=True,
        timeout=120,
        # The window runs the CLI with exactly this encoding environment.
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8")
    # the in-place lines, with their carriage returns intact
    assert "building 3/10 frames" + CR in out
    assert "building 10/10 frames" + CR in out
    # Windows text-mode stdout writes CRLF for a newline; the window treats a
    # segment with a newline in it as a finished line either way (gui.say).
    lines = out.replace(CR + NL, NL).split(NL)
    assert lines[0] == "Pulling 1 observation(s) to /tmp/x"
    assert any(ln.endswith("ingested 10 frames, 10 FITS") for ln in lines)
    # a long line is one line: the window parses whole paths off these
    long_lines = [ln for ln in lines if ln.startswith("  into ")]
    assert len(long_lines) == 1 and len(long_lines[0]) > 100
