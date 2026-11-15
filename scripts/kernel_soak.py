"""Compare the compiled kernel with the Python kernel on many generated cases.

    python scripts/kernel_soak.py FIRST_SEED COUNT          random cases
    python scripts/kernel_soak.py pairs [STEP]              every joker with every other
    python scripts/kernel_soak.py targeted [VARIANTS]       every joker over each enhancement

Prints each case where the two disagree (seed, or the joker pair) and a count at
the end. Run several ranges side by side for volume. Exits 1 on any mismatch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.kernel_cases import (  # noqa: E402
    SCORING_KEYS, kernel_search_result, pair_case, random_case, score_pairs, search_result, targeted_case,
)


def differs(case, typed: bool) -> str | None:
    if kernel_search_result(case, typed) != search_result(case, typed):
        return "search"
    for python, kernel in score_pairs(case, typed):
        if python != kernel:
            return "score"
    return None


def main() -> int:
    started = time.time()
    checked = bad = 0
    if sys.argv[1] == "pairs":
        keys = SCORING_KEYS[::int(sys.argv[2]) if len(sys.argv) > 2 else 1]
        cases = ((f"{a}+{b}/{v}", pair_case(a, b, v)) for a in keys for b in keys for v in range(5))
    elif sys.argv[1] == "targeted":
        variants = int(sys.argv[2]) if len(sys.argv) > 2 else 117
        cases = ((f"{key}/target {v}", targeted_case(key, v)) for key in SCORING_KEYS for v in range(variants))
    else:
        first, count = int(sys.argv[1]), int(sys.argv[2])
        cases = ((f"seed {seed}", random_case(seed)) for seed in range(first, first + count))
    for label, case in cases:
        for typed in (True, False):
            checked += 1
            what = differs(case, typed)
            if what:
                bad += 1
                if bad <= 20:
                    print(f"MISMATCH in {what}: {label} ({'typed' if typed else 'raw'})", flush=True)
    print(f"{checked} runs, {bad} mismatches, {time.time() - started:.0f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
