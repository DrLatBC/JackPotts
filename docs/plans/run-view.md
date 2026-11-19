# Run view

Status: step 1 done (2026-11-18). `RunView`, `run_view(state)`, `with_roster` and `joker_value(candidate, run)` are in `domain/policy/run_view.py` and `shop_valuation.py`. `SimContext.build(candidate, run)` and every caller (shop roster, shop buys, post-sell what-ifs, Diet Cola, Campfire's Hex check, Buffoon and Spectral packs, every Hex decision, the value map) moved over. `_get_deck_profile` now exists once, as `run_view.deck_profile`. The 23 reference seeds on the kernel gave identical actions (3,570) and logs.
Step 2 done (2026-11-18): every caller prices against the whole run view, and `tests/test_one_joker_price.py` replaces `test_blind_name_threading.py`. Measured on 1,000 paired Blue/White simulator seeds (R2S00001–R2S01000) on the kernel against step 1: 141 games played differently, first diverging at a shop buy, sell or leave in 124 of them and at a pack pick in 15. Wins +0.004 (se 0.004); final ante +0.060 (se 0.022), about 2.7 standard errors, so a real gain (48 changed games reached a later ante, 26 an earlier one). Rearranges, actions per game and speed unchanged.
Deviation from decision 3: `RoundContext` has no `ctx.run`. Nothing reads it, and building it on every round tick would cost a strategy and a deck profile. Callers read `run_view(state)`.

## Problem

`shop_valuation.evaluate_joker_value` takes 12 loose inputs, and each caller passes a different subset, so one joker gets a different joker value (see GLOSSARY.md) from the shop, a pack and Hex. That breaks ADR 0003's one price.

| Input | Shop (`shop_evaluator.py:210`, `:269`) | Pack pick (`pack_policy.py:207`) | Hex (`consumable_policy.evaluate_hex:353`, `rules/consumables.py:220`, `:277`) | Value map (`value_map.py:164`) |
|---|---|---|---|---|
| deck profile | yes | yes | no | vanilla |
| build includes the deck | yes | no (`rules/packs.py:368`) | no | no |
| joker slots | real | real | 5 | 5 |
| money | real | 0 | 0 | 20 |
| discards | 0 | 0 | 0 | 2 |
| play history (`_live_stats`) | yes | no | no | no |
| unique planets used | yes (`shop_evaluator.py:670`) | 0 | 0 | 0 |
| boss | none (blended) | current | current | none |

Results: Bull and Bootstraps are worth $0 outside the shop. Hex never counts Negative slots. Packs and Hex build their strategy without the deck. `tests/test_blind_name_threading.py` exists only to catch one missing input. `_get_deck_profile` exists twice (`shop.py:13`, `shop_evaluator.py:1131`).

## Game facts

- `G.FUNCS.cash_out` sets `current_round.discards_left` to the per-round count before the shop opens (`button_callbacks.lua:2971`). In the shop, `round.discards_left` is therefore the number of discards the next round starts with. Jackdaw matches the live game on this field: the lockstep bridge compares it.
- In a round, discards left plus `round.discards_used` gives the round's starting count. The fork's serializer sends both (`gamestate.lua:574-579`).

## Decisions

1. Glossary term **Run view**. Code type: `RunView`, a frozen dataclass with these fields:
   - roster
   - hand levels
   - ante
   - joker slots
   - deck profile
   - strategy, built with the deck
   - money
   - discards per round
   - play history (`LiveRunStats` or None)
   - unique planets used
   - boss (current blind name in a round, else None)
2. The interface is `joker_value(candidate, run)`. `run.with_roster(jokers)` handles what-ifs (sell first, then buy) and rebuilds the strategy for that roster. Nothing else is on the interface. `SimContext.build` takes `(candidate, run, monte_carlo_samples)`; it stays internal.
3. `run_view(state)` builds the view once per tick and caches it on the state, as `RoundContext.from_state` does. It runs no hand search. `RoundContext` exposes the same object as `ctx.run`. The shop evaluator, packs and Hex read it, and their own `compute_strategy` / `_get_deck_profile` calls for pricing are removed. The two `_get_deck_profile` copies become one.
4. The boss is set only in a round. The shop, packs and blind select carry none, so the shop's blend across likely bosses applies.
5. Hand levels are the run's real levels, never the boss-modified ones (The Flint, The Arm). The boss adjustment covers the current blind.
6. Discards are always the count a round starts with: `discards_left + discards_used` in a round, `discards_left` elsewhere.
7. The value map builds its fixed scenarios as synthetic run views with the same numbers it uses now ($20, 2 discards, vanilla deck, no play history, 5 slots, no planets), so its published values are unchanged.
8. Memo key: the candidate, its slot, and the run view. Money stays out of the key unless a money-scoring joker is involved, as it is now.

## Steps

1. **Same play.** Add `RunView`, `run_view(state)`, `with_roster`, and `joker_value(candidate, run)`. Move every caller onto them. Callers that don't pass a full picture today spell out their current omissions at the call site, using a temporary `run.as_before(...)` override. Merge `_get_deck_profile`. Proof: the 23 reference seeds on the kernel replay with identical action traces, and the targeted tests pass.
2. **One price.** Delete `as_before` so every caller prices against the full run view. Replace `test_blind_name_threading.py` with one test: the same joker in the same run gets the same price from the shop, a pack and Hex. Then run the paired measurement on the 1,000 seeds (R2S00001–R2S01000 @BLUE, kernel) against step 1, and record the results here.
