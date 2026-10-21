"""Build the synthetic seed workspace ahead of time: `python -m ledgerspy_demo.prebuild <folder>`.

Used when building the container image, so the 0.1-CPU host does not import the synthetic company at every
start. The folder is then given to the server as `LEDGERSPY_DEMO_SEED_DIR`; the server verifies it (audit
chain, triggers, evidence) and only reads it. Only `data/synthetic/reference/` is used.
"""

import sys
from pathlib import Path

from ledgerspy_demo.limits import DemoLimits
from ledgerspy_demo.seed import build_seed
from ledgerspy_demo.workspace import demo_config


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not Path(argv[0]).is_absolute():
        print("usage: python -m ledgerspy_demo.prebuild <absolute folder>", file=sys.stderr)
        return 2
    limits = DemoLimits()
    info = build_seed(Path(argv[0]), lambda path: demo_config(limits, path))
    print(f"seed ready: {len(info['datasets'])} synthetic datasets")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
