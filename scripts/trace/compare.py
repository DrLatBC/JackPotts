"""Compare two traces written by record.py.

    python scripts/trace/compare.py A.json B.json

Exits 0 when every action and every log line match, 1 otherwise.
"""
from __future__ import annotations

import json
import sys

SKIP = "Connected to balatrobot API"


def show(text: str) -> str:
    return text.encode("ascii", "replace").decode()


def main() -> int:
    a = json.load(open(sys.argv[1], encoding="utf-8"))
    b = json.load(open(sys.argv[2], encoding="utf-8"))

    ta, tb = a["trace"], b["trace"]
    first = next((i for i, (x, y) in enumerate(zip(ta, tb)) if x != y), None)
    same_actions = first is None and len(ta) == len(tb)
    if same_actions:
        print(f"ACTIONS IDENTICAL ({len(ta)})")
    else:
        print(f"ACTIONS DIFFER: {len(ta)} vs {len(tb)} entries, first difference at {first}")
        if first is not None:
            for i in range(max(0, first - 2), min(len(ta), len(tb), first + 3)):
                print(show(f"  A {ta[i]}\n  B {tb[i]}"))

    la = [x for x in a["log"] if SKIP not in x]
    lb = [x for x in b["log"] if SKIP not in x]
    diffs = [(i, x, y) for i, (x, y) in enumerate(zip(la, lb)) if x != y]
    print(f"log lines {len(la)} vs {len(lb)}; differing lines {len(diffs)}")
    for i, x, y in diffs[:8]:
        print(show(f"  [{i}] A: {x[:200]}\n       B: {y[:200]}"))
    return 0 if same_actions and not diffs and len(la) == len(lb) else 1


if __name__ == "__main__":
    sys.exit(main())
