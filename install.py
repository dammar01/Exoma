"""Source-checkout compatibility: `python install.py ...` runs `exoma install ...`.

Workspaces scaffolded by agent-workflow call this file by absolute path; `exoma upgrade`
rewrites them to `python -m exoma`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from exoma.install import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
