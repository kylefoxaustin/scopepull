"""The ONLY place OS-specific code lives. Everything degrades gracefully:
on an unsupported OS or missing tool, functions return empty results, never raise.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .windows import current_ssids
else:
    from .linux import current_ssids

__all__ = ["current_ssids"]
