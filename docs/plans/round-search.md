# Round search

Status: done (2026-11-17).
- Step 1: every caller moved with its exceptions spelled out; 23 reference seeds gave identical actions on the Python scorer and the kernel. The Mouth commit samples stay per-sample searches: a miss needs the hand type, and a target hand type rules out the kernel's batch call anyway.
- Step 2: the exceptions are gone. The only relaxation is `ignore_mouth=True` (the Mouth fallback); `rules/consumables.py` reads `ctx.best`; the guard test has no allowlist. Measured as 1,000 paired Blue/White simulator seeds on the kernel instead of a dashboard batch (same seeds, step 1 code vs step 2 code): 128 games played differently, wins -0.001 (se 0.004), final ante +0.028 (se 0.016), same speed. The changed reference games traced to the intended fixes (consumables under The Flint; Mouth commit and discard EV under The Mouth).

## Problem

Every policy that looks for a hand calls `best_hand` / `enumerate_hands` (20 keyword parameters) and copies its own subset of the round's rules out of `RoundContext`. There are 16 production call sites, and most of them drop a rule:

| Call site | Changes | Drops |
|---|---|---|
| `context.py` best_as_finisher | hands_left=1 | Idol |
| `hand_sequencing.py:298` Eye plan | — | slot limit, forced card, blind, Idol, Ancient, deck |
| `hand_sequencing.py:406, 455` setup / scoring | target type | slot limit, blind, Idol, Ancient, Mouth |
| `hand_sequencing.py:656` To-Do List | target type | slot limit, Eye, blind, Idol |
| `hand_sequencing.py:965` Card Sharp | — | slot limit, Eye, forced card, blind |
| `play_policy.py:53` Ox | + exclude Ox hand | slot limit, forced card |
| `play_policy.py:241` Mouth fallback | drops Mouth lock | forced card, blind, Idol, Ancient, Eye |
| `play_policy.py:513` Space / Seance | target type | slot limit, Eye, hands_left, blind |
| `discard_policy.py:229` play EV | kept + drawn, adjusted jokers, discards-1 | Mouth, Eye, slot limit, Ox |
| `mouth_commit.py` x3 | target type, drawn cards | Eye |
| `rules/consumables.py:101` | raw state | nearly all |

Six sites pass `joker_limit=len(ctx.jokers)`, so Joker Stencil sees no empty slots there. `sort_play_order(..., idol_rank=, idol_suit=, ancient_suit=)` is repeated at 20 sites and `pad_with_junk(..., protection=ctx.card_protection)` alongside it.

## Decisions

1. A **round search** object (see GLOSSARY.md) holds the round's rules: hand levels, jokers, slot limit, money, hands/discards left, minimum cards, blind, forced card, Eye exclusions, Mouth commit, Idol, Ancient, Ox, deck, hand affinity. `from_snapshot` builds it once and the context holds it as `ctx.search`. The three searches inside `from_snapshot` use it too.
2. Callers name only what differs: cards, hands_left, discards_left, jokers, an extra excluded hand, a target hand type. Relaxing a round rule needs a keyword that names it (`ignore_mouth=True`); there is no general way to blank rules.
3. The forced card is tracked as the card itself, not a hand position. The search finds it in whatever card list it is given; absent means no constraint (kept cards in the discard EV, best_without_forced).
4. Every dropped rule in the table is a bug except the Mouth fallback dropping the Mouth lock; that fallback still keeps the forced card. `rules/consumables.py` uses `ctx.best`.
5. Play order and junk padding go through the round search: a chosen play comes back padded and ordered, ready to send.
6. `best_hand` / `enumerate_hands` stay in `search.py` as the implementation. Scoring tests and the kernel differential test keep calling them. A guard test stops `domain/policy/` and `rules/` importing them.
7. Mouth commit: build the search without a commit, run `choose_mouth_commit` on it, then lock the search to the chosen type. `mouth_commit._ChaseParams` is deleted.
8. The round search offers best totals over many card lists, choosing the kernel or Python inside. The discard EV and the Mouth commit samples use it and stop building their own `KernelScorer`. The shop valuation's kernel branches (candidate 6) are out of scope.

## Steps

1. **Move, no behaviour change.** Add the round search; move every call site onto it while passing exactly what it passes today (the exceptions spelled out explicitly at each site). Proof: seeded simulator games give identical action traces old vs new, on the Python scorer and the kernel.
2. **Apply the round rules.** Remove the per-site exceptions per decision 4. New test file builds `RoundContext`s and checks through `ctx.search`: Stencil with 2 free slots, Card Sharp under The Eye, discard EV under The Mouth, forced card in the Ox and Mouth fallbacks, Idol in the finisher. Measure with a batch against the step 1 baseline.
