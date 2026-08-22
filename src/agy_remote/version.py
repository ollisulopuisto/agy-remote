"""The one place this package's version is written.

CalVer, `YY.MM.DD.build`. Four copies of this string used to drift: `--version`
still reported a build from eighteen releases earlier, so a bug report could
not be tied to what was actually running. Every other module reads it here, and
`tests/test_version.py` holds pyproject to it.
"""

from __future__ import annotations

VERSION = "26.08.22.37"

#: Display form, as printed by `--version` and shown in the PWA.
__version__ = f"v{VERSION}"
