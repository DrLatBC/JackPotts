# Roster order

Status: step 1 done (2026-11-18): `roster_order` and `describe_order` in `domain/policy/roster_order.py`, `copy_target_score` moved there from shop_valuation; the three callers switched; `tests/test_roster_order.py` (13 cases, each also checked for stability). The 23 reference seeds on the kernel: no errors, 1 game played differently (a shop valuation), same results, 148 rearranges before and after.
Step 2 done (2026-11-18): 1,000 paired Blue/White simulator seeds on the kernel (R2S00001–R2S01000), step 1 code vs 3d4e525. 140 games played differently; wins +0.003 (se 0.002), final ante +0.005 (se 0.006): no measurable change in results. Rearranges 7.90 per game vs 8.09 (−0.19, se 0.07), all of the drop in rounds (3,092 vs 3,307); shop 4,466 vs 4,435. Same actions per game and same speed. The busiest games still make 30–40 in-round rearranges; unverified whether these are Dagger rosters moving between the shop's placement and the round's (decision 3).

## Problem

Three modules each work out the roster order (see GLOSSARY.md) and give different answers:

| | In-round rule (`rules/shop.py` ReorderJokersForScoring) | Shop plan (`shop_evaluator._compute_optimal_order`) | Valuation sim (`scoring_phase.reorder_for_scoring`) |
|---|---|---|---|
| Base sort | phase, edition, position | same | same |
| Blueprint target | rightmost xmult, else rightmost +mult | highest copy-target score | rightmost xmult, else leftmost +mult |
| Brainstorm | last; first useful joker swapped into slot 0 | last; best copy target moved to slot 0 | as in-round |
| Dagger fodder | any phase-0 joker, first by key | the shop's EV pick, else as in-round | none |
| Dagger, no fodder | rightmost | rightmost | sorts as +mult |

The shop sets an order, the next in-round tick undoes it, and the valuation prices a third. None has a direct test. Phase 0 covers retrigger and utility jokers, so with Chicot and Dagger owned Chicot is placed right of Dagger (read from code, not reproduced). Ordering logic in `rules/` goes against ADR 0006.

## Game facts (game-dump `card.lua`, Steamodded `utils.lua`)

- Photograph, Triboulet, Ancient Joker, Bloodstone and The Idol fire per scored card (`context.individual`, card.lua:3392); Baron fires per held card. All of these come before the joker phase (`joker_main`), so their slot only matters relative to other per-card jokers on the same card.
- Brainstorm copies `G.jokers.cards[1]`; Blueprint copies the joker to its right. `SMODS.blueprint_effect` follows chains (Blueprint is `blueprint_compat`), so Brainstorm can copy through Blueprint.
- A joker-phase xmult in slot 0 multiplies the bare starting mult; Tyler: copy xmult, but don't put it first.

## Decisions

1. `domain/policy/roster_order.py`: `roster_order(jokers, fodder=None, blind_next=False) -> list[int]`, a pure function returning new positions. The in-round rule, the shop plan and the valuation sim call it; the three implementations are deleted. The Amber Acorn skip stays in the rule.
2. Only the shop picks fodder (its EV pick). With no `fodder`, nothing is placed right of Dagger on purpose.
3. `blind_next=True` (shop): Dagger sits left of the fodder, or rightmost with none. `blind_next=False` (round, valuation): Dagger sorts as +mult. A skipped blind reaches blind select without a shop; accepted.
4. Blueprint goes just left of the joker with the highest copy-target score (`_copy_target_score`) everywhere.
5. Brainstorm goes last. Slot 0 takes, in order: the best per-card xmult (Photograph, Triboulet, Ancient, Bloodstone, Baron, The Idol), else the best +mult/+chips joker, else the phase sort's own first joker. A joker-phase xmult never moves to slot 0. Bloodstone and Baron join the per-card set in `scoring_phase.py`.
6. The order is stable: ordering an ordered roster returns it unchanged, tested over every table case. Both `_last_order` guards go. The shop's Dagger tie check stays: it guards which fodder is picked (ties by index swap after a move), not the order.
7. Measured like the round search: 1,000 paired simulator seeds against the current commit, plus rearrange actions per game, old vs new.

## Steps

1. Add `roster_order` with a table test (roster → order) covering each row above, the Chicot case and stability; switch the three callers; delete the old code.
2. Paired-seed measurement and rearrange counts; record results here.
