#!/usr/bin/env python3
"""Entry point for the frozen scopepull.exe.

    scopepull.exe                -> the owl (window)
    scopepull.exe <folder>       -> the owl, with that folder in the box
    scopepull.exe --cli ...      -> the plain command line, no window

The window runs every pull, index and combine by re-launching this same exe
with --cli, so the CLI stays the one source of truth and the exe needs no
Python on the machine.
"""

import multiprocessing
import sys


def main() -> None:
    multiprocessing.freeze_support()  # required for worker processes in a frozen exe
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from scopepull.cli import run

        sys.argv = ["scopepull", *sys.argv[2:]]
        run()
        return
    from scopepull.gui import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
