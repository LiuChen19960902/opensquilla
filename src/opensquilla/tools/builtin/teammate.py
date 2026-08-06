"""Register teammate tools (thin wrapper over ``opensquilla.teammate``).

Kept under ``tools/builtin`` so the builtin loader's ``_NAMES`` loop imports
it like any other tool module; the real implementation lives in the teammate
subsystem package.
"""

from __future__ import annotations

from opensquilla.teammate import tools as _tools  # noqa: F401 — side-effect: register tools
