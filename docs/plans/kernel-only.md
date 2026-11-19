# Kernel only

Status: steps 1 and 2 done (2026-11-18). Architecture item #6 (scorer seam) and stage 5 of the compiled kernel plan. Python and the kernel agreed on all 2,401 saved cases, typed and raw, before the Python scorer was deleted. The 23 reference seeds replay identically to a `BALATRO_KERNEL=rust` recording of HEAD after each step (4,012 actions, 16,392 log lines), and 849 tests pass.

Deviations:
- Decision 9: `kernel_expected.json` (1.9 MB) keeps each case's search as a play count, a digest and the best play, plus the scored plays in full. The digest treats every number as a float, because the kernel returns whole chips as ints where Python had floats.
- Decision 8: no ordering-only comparison existed. `test_kernel_play_order_matches_the_play_orderer` is new: the two orders agree on every joker lineup in the generated cases. They differ only on no-joker hands, where order does not change the score.
- Rebased onto #7 (`d19909d`). The interpreter rule now lives in `game_server`: both `bot_python()` methods return `venv_python("bot")` and take no kernel flag, and the `"pypy"` entry left `config._VENVS`. The trace is still identical after the rebase.
- Also removed: the scoring functions' `deck_cards` argument (only the Python scorer read it), `PerCardPlan.ops_for` / `_compile`, `CardFacts.base_ops` / `hack_rank`, and the `OP_*` step codes.
- `CLAUDE.md` is untracked and was edited in the main checkout. The new kernel (`joker_phases`, per-card triggers) is not installed into `.venv`, and the tests ran on a wheel unpacked to a scratch folder. Install it only when this lands on master, between batches: master's adapter expects the old five-value `breakdown`, so the new build would break master's scoring log.

## Problem

Hand search and scoring exist twice: the Python scorer and the compiled kernel (`kernel/`, `domain/scoring/kernel.py::KernelScorer`). `estimate.USE_KERNEL`, read once from `BALATRO_KERNEL=rust`, picks between them and is off by default. Batches turn it on through the panel's KERNEL box (`gui.py:51`, `:195`, `:1868-1887`, `:2022`) → supervisor `--kernel` (`supervisor.py:1314`, `:1330`) → env var in each bot (`supervisor.py:820`). Unit tests, a bot started from a shell, and rl run the Python scorer. The switch is read at `estimate.py:216`, `:552`, `search.py:253`, `:301`, `round_search.py:149` and `shop_valuation.py:1245`.

Simulator bots run on PyPy (`.venv-pypy`, ADR 0010) only when the kernel is off (`supervisor.py:585`). Batches always have it on, so the PyPy path is never used.

## Code facts

- `KernelScorer` reads per-lineup facts from Python: `parsers` (`_ab_chips`, `_ab_mult`, `_ab_xmult`, `_ability`, `_current`), `roster` (`Roster`, `copy_target`, `roster`), `registry._effect_plan` (edition numbers and Baseball only, `kernel.py:243`; it ignores the effect field), and `estimate._before_phase_jokers` / `ox_most_played_hand` (`kernel.py:33-36`).
- Four places outside the scorer use Python scoring code:
  - `shop_valuation._has_scoring_effect` (`:1067`) asks `JOKER_EFFECTS` whether a joker scores in the joker phase.
  - `scoring_phase._build_from_simple_table` (`:40`) gets each table joker's phase (chips / mult / xmult) from `simple.SIMPLE_EFFECTS_TABLE`. `roster_order` and `shop_valuation` read those phases.
  - The mismatch dump in `bot_logging.py:530-581` builds a `ScoreContext` to call `retrigger_count` for each card's trigger count.
  - `tests/test_joker_valuation.py:719` checks `_MONEY_SCORING_KEYS` by reading `ctx.money` in the source of `complex.py` / `simple.py`. `tests/kernel_cases.py:41` builds `SCORING_KEYS` from `JOKER_EFFECTS`.
- `play_order.py` (Python `PlayOrderer`) uses `per_card` and `roster`, not the scorer. rl imports `play_order` and `strategy`, which pull in `joker_effects` (`registry`, `simple`, `complex`, `context` through the package `__init__`) but never `estimate` or the kernel. Checked in `.venv-rl`.
- `.venv`, `.venv-rl` and `.venv-jackdaw` are all Python 3.13. The only 3.12 interpreter is `.venv-pypy`.
- The #7 game-server session moves the bot-interpreter rule from `supervisor.py` to `game_server.Jackdaw.bot_python(kernel)` and `config.venv_python(...)`. Its note says whichever change lands second simplifies that to CPython.

## Decisions

1. **Delete the Python per-hand scorer.** Gone:
   - `HandScorer`'s Python path: `_ctx`, `score_cards`, `_hand_base`, `use_facts` and the no-joker shortcut.
   - `_held_phase_jokers` and `_apply_before_phase`.
   - The Python breakdown in `score_hand_detailed`.
   - `search._scored_subsets` and the Python loops in `enumerate_hands` / `best_hand`.
   - The Python sample loop in `shop_valuation` (`:1258-1271`).
   - `joker_effects/simple.py`, `complex.py`, `context.py` and `registry.py`.
   `HandScorer`, `score_hand`, `score_hand_detailed`, `enumerate_hands` and `best_hand` stay as the scoring interface. They are thin over `KernelScorer`, so callers and tests don't change. `score_hand_detailed` keeps building `card_details` (labels and card numbers) in Python.
2. **Kept:** `parsers`, `live_values`, `roster`, `per_card` (`PlayOrderer` uses it), `scoring_phase`. `_effect_plan` becomes an edition plan without the effect field and moves into `kernel.py`, which is its only reader. So do `_before_phase_jokers` and `ox_most_played_hand`, so `kernel.py` no longer imports `estimate`. Whatever else the deletion turns up as still used outside the scorer stays and is listed here at build time.
3. **The kernel owns "which jokers score, and how".** `jackpotts_kernel` exports `joker_phases()`: every joker key with a joker-phase effect, mapped to `chips`, `mult`, `xmult` or `other`. Table rows get their phase from their row kind; hand-written effects get `other`. Three readers use it:
   - `scoring_phase` builds its table part from it. `_COMPLEX_PHASES` keeps its hand-written phases.
   - `shop_valuation._has_scoring_effect` checks it.
   - `kernel_cases.SCORING_KEYS` is built from it.
   Before deleting, snapshot the current `get_joker_phase` map and the `_has_scoring_effect` answer for every key the kernel or Python knows. A test then checks that the new answers match the snapshot.
4. **The mismatch dump reads trigger counts from the kernel.** `KernelScorer.breakdown` returns each scoring card's trigger count, already computed in Rust. `bot_logging` logs those and drops `ScoreContext` / `retrigger_count`.
5. **Money test by behaviour.** `test_every_joker_that_scores_off_money_is_listed` scores each key in `joker_phases()` through the kernel at $0 and $50. The keys whose score changes must equal `_MONEY_SCORING_KEYS`.
6. **Kernel always on.** Remove:
   - `USE_KERNEL` and the `BALATRO_KERNEL` env var.
   - Supervisor `--kernel`, its env var, its pause-state field and the startup runtime lines.
   - The panel KERNEL checkbox and the `kernel` setting. Old `gui_settings.json` and pause files with the key load fine; the key is ignored.
   - `PYPY_PYTHON` / `$BOT_PYPY` and the PyPy choice.
   Sim bots run on `.venv` CPython. If #7 has landed first, this change simplifies `Jackdaw.bot_python` and drops the `"pypy"` interpreter from `config.venv_python` instead.
7. **No kernel → fail at import.** `kernel.py` turns a failed `import jackpotts_kernel` into an `ImportError` that names the build step in `docs/installation.md`. There is no fallback. rl stays kernel-free because it never imports `estimate`.
8. **Play ordering stays twice for now.** `PlayOrderer` stays, and its ordering-only differential test stays. "One play ordering: kernel in `.venv-rl`, DNA preferences into the kernel" is a separate follow-up.
9. **Tests.**
   - While both scorers exist, save `kernel_cases`' outputs as `tests/kernel_expected.json`: random seeds, pair cases and targeted cases, with scores, breakdowns and search results, after asserting Python and the kernel agree on every one. If the file comes out over ~2 MB, keep the random seeds and targeted cases and drop the pair cases.
   - `test_kernel_cases.py` checks the kernel against the file, and its self-consistency tests stay.
   - Delete `test_kernel_differential.py` (except the ordering test), `scripts/kernel_soak.py`, `scripts/kernel_mutations.py`, `scripts/kernel_coverage.py` (it measures coverage of the deleted Python files) and the Python runners in `kernel_cases.py`.
   - Every score-checking test runs through the kernel. The live game stays the real check, through `/mismatch`.
10. **The Python 3.12 rule goes with PyPy.** All three remaining environments are 3.13. The PyPy test command and the "PyPy orders sets by insertion" note leave `balatrobot/references/internals.md`. The rule against letting decisions depend on set order stays: it's about seeded replays, not PyPy.
11. **Docs.**
    - ADR 0010 gets a superseded note: kernel only, sim bots on CPython.
    - `docs/installation.md` loses the PyPy section, and the kernel build moves from optional to required.
    - Also updated: `docs/architecture.md`, `CLAUDE.md`, `README.md`, `docs/release-notes.md`, `GLOSSARY.md` if any entry names the Python scorer, the `batch` / `balatrobot` / `test` / `knobs` / `mismatch` skills, and the `project_compiled_kernel_plan` memory note (stage 5 done).

## Steps

1. **Same play, kernel everywhere.**
   - Add `joker_phases()` and per-card trigger counts to the kernel, and rebuild it (between batches; a running batch holds the `.pyd`).
   - Write `tests/kernel_expected.json` and the phase / scoring-effect snapshot, checked against both scorers.
   - Make `USE_KERNEL` always true and have the supervisor always set the env var.
   - Run the full suite.
   - Proof: the 23 reference seeds (`scripts/trace/seeds.txt`) recorded with `scripts/trace/record.py` against a Jackdaw server give action lists identical to a `BALATRO_KERNEL=rust` recording of HEAD, comparing with PAYLOAD entries removed (`scripts/trace/compare.py`).
2. **Delete.**
   - Delete everything in decisions 1 and 6, and move the readers in 3-5 onto the kernel.
   - Delete the comparison tools in 9.
   - Update the docs in 10-11.
   - Proof: the 23-seed trace again, identical to step 1, and the full suite.
   - No 1,000-seed run: batches already score with the kernel, so play doesn't change.
