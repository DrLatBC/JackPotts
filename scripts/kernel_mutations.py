"""Check that the differential test notices a wrong kernel.

    python scripts/kernel_mutations.py

Plants one small bug at a time in the Rust kernel, rebuilds it, and runs
tests/test_kernel_differential.py, which should fail each time. A bug the quick
test misses is then put to the longer targeted soak (scripts/kernel_soak.py),
which must catch it. The source is restored and the kernel rebuilt at the end. Needs cargo and maturin on PATH and
no other process using the kernel while it runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "kernel" / "core" / "src"

# (file, text as it is, text with the bug, what the bug is)
MUTATIONS = [
    ("lineup.rs", "if card.is(RED_SEAL) {\n                    triggers += 1;", "if card.is(RED_SEAL) {\n                    triggers += 0;",
     "a Red Seal no longer retriggers"),
    ("lineup.rs", "if best.map_or(true, |(_, top)| value > top) {", "if best.map_or(true, |(_, top)| value >= top) {",
     "play order takes the last of equal cards instead of the first"),
    ("jokers.rs", "if s.held.iter().all(|c| c.suits_under(around.smeared) & (SPADES | CLUBS) != 0) {",
     "if s.held.iter().all(|c| c.suits_under(around.smeared) & SPADES != 0) {", "Blackboard no longer counts Clubs"),
    ("hands.rs", "add_of_rank(&mut picked, cards, rank_with(counts, 2, trips), 2);",
     "add_of_rank(&mut picked, cards, rank_with(counts, 2, 0), 2);", "Full House takes its pair from the trips' rank"),
    ("search.rs", "let (first, second) = if self.no_jokers { (hand, total) } else { (total, hand) };",
     "let (first, second) = (total, hand);", "without jokers the total outranks the hand type"),
    ("jokers.rs", "CardEffect::ChipsPerRetrigger(value) => *chips += value * trigger as f64,",
     "CardEffect::ChipsPerRetrigger(value) => *chips += value * (trigger + 1) as f64,",
     "Hiker counts on a card's first trigger"),
    ("lineup.rs", "if card.rank != 0 && card.rank <= lowest {", "if card.rank != 0 && card.rank < lowest {",
     "Raised Fist takes the first of equal lowest cards instead of the last"),
    ("jokers.rs", "let chance = if around.oops { 2.0 / 5.0 } else { 1.0 / 5.0 };", "let chance = 1.0 / 5.0;",
     "Lucky Cat ignores Oops! All 6s"),
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def build() -> bool:
    env = {**os.environ, "VIRTUAL_ENV": str(Path(sys.executable).parents[1])}
    maturin = Path(sys.executable).with_name("maturin.exe" if os.name == "nt" else "maturin")
    done = run([str(maturin), "develop", "--release"], cwd=ROOT / "kernel" / "py", env=env)
    if done.returncode:
        print(done.stderr[-2000:])
    return done.returncode == 0


def differential_passes() -> bool:
    done = run([sys.executable, "-m", "pytest", "-q", "-x", "tests/test_kernel_differential.py"], cwd=ROOT)
    return done.returncode == 0


def soak_passes() -> bool:
    done = run([sys.executable, "scripts/kernel_soak.py", "targeted"], cwd=ROOT)
    return done.returncode == 0


def main() -> int:
    missed = 0
    try:
        for name, good, bad, what in MUTATIONS:
            path = CORE / name
            source = path.read_text(encoding="utf-8")
            if source.count(good) != 1:
                print(f"SKIPPED (source changed, update this script): {what}")
                missed += 1
                continue
            path.write_text(source.replace(good, bad), encoding="utf-8", newline="\n")
            try:
                if not build():
                    print(f"DID NOT BUILD: {what}")
                    missed += 1
                elif not differential_passes():
                    print(f"caught by the quick test: {what}")
                elif not soak_passes():
                    print(f"caught by the soak only: {what}")
                else:
                    print(f"NOT CAUGHT: {what}")
                    missed += 1
            finally:
                path.write_text(source, encoding="utf-8", newline="\n")
    finally:
        restored = build()
    ok = restored and differential_passes()
    print("kernel restored and passing" if ok else "KERNEL NOT RESTORED CLEANLY")
    return 1 if missed or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
