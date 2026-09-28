#!/usr/bin/env python3
"""Convert a vendor EZP2019+ chip database (EZP2019+.Dat) to our JSON format.

Usage: tools/dat2json.py EZP2019+.Dat [-o ezp2019linux/data/chips.json]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ezp2019linux.core.chipdb import BUNDLED_DB, parse_dat, write_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dat", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=BUNDLED_DB)
    args = ap.parse_args()

    chips = parse_dat(args.dat.read_bytes())
    write_json(args.output, chips,
               "Chip list converted from the EZP2019+ Windows software "
               f"database ({args.dat.name})")
    print(f"wrote {len(chips)} chips to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
