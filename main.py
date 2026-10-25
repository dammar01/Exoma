"""Source-checkout compatibility: `python main.py ...` runs `exoma ...`.

Workspaces scaffolded by agent-workflow call this file by absolute path; `exoma upgrade`
rewrites them to `python -m exoma`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from exoma.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
