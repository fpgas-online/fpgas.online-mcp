"""The project-local scratch directory the tests use (never /tmp).

`FPGAS_ONLINE_TEST_TMP` overrides it: a checkout at a long path (a git worktree, say) would make the
broker's Unix socket path in it longer than the 107 bytes a socket allows.
"""

import os
from pathlib import Path

TEST_TMP = Path(os.environ.get("FPGAS_ONLINE_TEST_TMP") or Path(__file__).resolve().parent.parent / "tmp")
