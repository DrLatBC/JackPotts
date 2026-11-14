# Plan: structured joker values (no effect-text parsing), and a Jackdaw adapter

Goal: the bot reads every number it needs from structured gamestate fields, so the live game (balatrobot fork) and the simulator (Jackdaw + adapter) feed it through one path. Effect text stays only for logs.

## What the bot gets from text today

The bot already prefers `value.ability` and falls back to text (`joker_effects/parsers.py` `_ab_chips/_ab_mult/_ab_xmult`). The text-only numbers are the ones the game builds for display in `Card:generate_UIBox_ability_table` (vanilla `card.lua:708-948`) or recomputes every frame in `Card:update` (`card.lua:4126`), and the fork doesn't copy into `value.ability`:

| Joker | Displayed value | Source in the game | Reaches the bot today via |
|---|---|---|---|
| Steel Joker | X(1 + extra*steel_tally) | `ability.steel_tally`, counted over `G.playing_cards` in `Card:update` (card.lua:4185) | text (`complex.py:345`) |
| Stone Joker | +extra*stone_tally chips | `ability.stone_tally`, card.lua:4197 | text |
| Cloud 9 | $extra*nine_tally | `ability.nine_tally`, card.lua:4191 | text |
| Driver's License | tally of enhanced cards | `ability.driver_tally`, card.lua:4179 | already structured |
| Yorick | discards left to next X1 | `ability.yorick_discards` (card.lua:328, 918) | text (`complex.py:374`, `sim_context.py:125`) |
| To Do List | target hand | `ability.to_do_poker_hand` (card.lua:316-321) | not used |
| Blue Joker | +extra*#deck chips | `#G.deck.cards` (card.lua:753) | text |
| Abstract Joker | +extra*#jokers mult | `#G.jokers.cards` (card.lua:788) | text |
| Erosion | +extra*(starting_deck_size - #playing_cards) | `G.GAME.starting_deck_size`, `#G.playing_cards` (card.lua:878) | text |
| Bull | +extra*max(0, dollars) chips | `G.GAME.dollars` (card.lua:885) | text |
| Bootstraps | +mult*floor((dollars + dollar_buffer)/dollars_step) | card.lua:915 | text |
| Fortune Teller | +extra*tarots used | `G.GAME.consumeable_usage_total.tarot` (card.lua:743) | text |
| Satellite | $extra*unique planets used | `G.GAME.consumeable_usage` (card.lua:909-911) | text |
| chance jokers (8 Ball, Space, Gros Michel, Cavendish, Bloodstone, Business Card, Reserved Parking, Hallucination…) | "N in M" | `G.GAME.probabilities.normal` | text where parsed |
| any debuffed joker | "All abilities are disabled" | debuff | text + `state.debuff` (`cards.py:75-79`) |

Scaling jokers whose running value is stored (Ride the Bus, Obelisk, Popcorn, Ramen, Flash Card, Swashbuckler, Throwback, Joker Stencil…) are already in `ability.mult`/`x_mult`/`extra` and the bot reads them structured; the text fallback for them goes away.

## Design

One rule: the gamestate sends **raw game counters**, the bot holds **the formulas** (each citing its card.lua line). No formula lives in two places.

New gamestate fields (fork and adapter send the same names and values):

- `value.ability` gains `steel_tally`, `stone_tally`, `nine_tally`, `yorick_discards`, `to_do_poker_hand` (the fork copies them from `card.ability`; the adapter counts the tallies over the snapshot's playing cards in deck + hand + discard + play, the same set as `G.playing_cards`).
- Top-level `counters`: `probabilities_normal`, `tarots_used` (`consumeable_usage_total.tarot`), `planets_used_unique`, `starting_deck_size`, `playing_cards` (`#G.playing_cards`), `dollar_buffer`.
- The adapter also fills the fork-only fields found in the field diff: `round.ancient_suit`, `round.idol_card`, `round.most_played_poker_hand`, `value.rarity`, `value.ability.*` (the fork's whitelist: extra flattened, t_mult, t_chips, mult, x_mult, driver_tally, loyalty_remaining), `modifier.edition_chips/edition_mult/edition_x_mult/enhancement_x_mult`.

## Steps

1. **Bot**: one module (`joker_effects/live_values.py`) computes each table row from ability + counters; replace every text read with it: `parsers._ab_*` fallbacks, `complex.py:345,374`, `shop_valuation.py:1456,1463,1976`, `shop_evaluator.py:1151`, `scaling_projection.py:250`, `sim_context.py:115-125`, `cards.py:75-79` (use `state.debuff` only), `value_map._synthesize_effect`. `parse_effect_value` stays for logs only. Tests: one per row, values from the card.lua formula.
2. **Fork** (`C:\github vs\balatrobot`, DrLatBC/balatrobot): add the ability keys and `counters` in `src/lua/utils/gamestate.lua`.
3. **Adapter** (`sim/jackdaw_adapter/` here, runs in its own Python 3.12+ venv with jackdaw): wraps `SimBackend`, adds the fields from `Game.to_dict()`, serves balatrobot's HTTP API so the bot runs unchanged.
4. **Verify**: lockstep a recorded bot game on headless real-game Lua (fork fields) vs adapter, now comparing every field the bot reads, including the new ones; then bot games on both with no API errors.
5. Docs: ADR 0007 update (Jackdaw replaces "bare Lua first"; the headless Lua is the exact cross-check), balatrobot skill notes.

Out of scope here: `earnings[]` (dashboard only), bot speed, in-process `SimBackend` (no HTTP).

## Open questions for Tyler

1. Are the live bot profiles fully unlocked? Jackdaw assumes they are; a locked profile changes shop and pack pools.
2. OK to change the fork (step 2) in the same push as the bot (step 1)? The bot would require the new fields, so an old fork build would break it.
