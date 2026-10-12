"""`python -m scopepull` — the CLI.

The window runs the CLI as a subprocess (so a pull can be stopped, paused and
killed with everything under it), and `python -m scopepull` is how it does that
when it isn't the frozen exe: the interpreter running the window is by
definition one that can import scopepull, which is more reliable than hunting
for a console script on a PATH that Explorer inherited at sign-in time.
"""

from __future__ import annotations

from .cli import app

if __name__ == "__main__":
    app()
