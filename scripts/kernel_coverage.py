"""How much of the scoring kernel the generated cases in tests/kernel_cases.py reach.

    python scripts/kernel_coverage.py [RANDOM_CASES] [PAIR_STEP]

Runs the cases under coverage.py with branch tracking and prints, per kernel file,
the share of lines and branches reached and the lines never run. Needs the
``coverage`` package (in the dev extras).
"""
from __future__ import annotations

import sys
from pathlib import Path

import coverage

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "balatro_bot"
KERNEL = [
    SRC / "cards.py",
    *(SRC / "domain" / "scoring" / name for name in ("search.py", "classify.py", "estimate.py", "play_order.py")),
    *(SRC / "joker_effects" / name for name in (
        "registry.py", "simple.py", "complex.py", "per_card.py", "context.py", "roster.py")),
]


def main() -> None:
    random_cases = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    pair_step = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    sys.path.insert(0, str(ROOT))

    cov = coverage.Coverage(branch=True, include=[str(p) for p in KERNEL], data_file=None)
    cov.start()
    from tests.kernel_cases import SCORING_KEYS, pair_case, random_case, score_results, search_result

    for seed in range(random_cases):
        case = random_case(seed)
        for typed in (True, False):
            search_result(case, typed)
            score_results(case, typed)
    keys = SCORING_KEYS[::pair_step]
    for first in keys:
        for second in keys:
            for variant in range(5):
                case = pair_case(first, second, variant)
                search_result(case, typed=True)
                score_results(case, typed=False)
    cov.stop()

    print(f"{random_cases} random cases, {len(keys) ** 2 * 5} pair cases")
    total_branches = reached_branches = 0
    for path in KERNEL:
        analysis = cov._analyze(str(path))
        numbers = analysis.numbers
        total_branches += numbers.n_branches
        reached_branches += numbers.n_branches - numbers.n_missing_branches
        lines = 100.0 * (numbers.n_statements - numbers.n_missing) / max(1, numbers.n_statements)
        branches = 100.0 * (numbers.n_branches - numbers.n_missing_branches) / max(1, numbers.n_branches)
        missing = analysis.missing_formatted()
        print(f"{path.relative_to(SRC)!s:34} lines {lines:5.1f}%  branches {branches:5.1f}%"
              + (f"  never run: {missing}" if missing else ""))
    print(f"branches reached: {reached_branches}/{total_branches} "
          f"({100.0 * reached_branches / max(1, total_branches):.1f}%)")


if __name__ == "__main__":
    main()
