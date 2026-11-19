# Shop producers

Status: steps 1 and 2 done (2026-11-18). Item #8 of the 2026-11-17 architecture review. `ShopView`, the eleven producers in `_PRODUCERS`, `ActionPlan.kind`, `ranked_plans` and `_remember_invisible` are in `shop_evaluator.py`, and `evaluate` is collect → rank → emit. The 23 reference seeds on the kernel gave identical actions (4,012, PAYLOAD entries removed) and identical logs (16,392 lines) against HEAD 92d3ba7, both after step 1 and again after step 2. `tests/test_shop_plans.py` (15 tests) asserts on ranked plans. Full suite: 869 passed.
Deviation from decision 1: the view stores `slots_full` and derives `actual_count` and `slots_open` as properties.

## Problem

`ShopEvaluator.evaluate` (`shop_evaluator.py:597-1007`) is one ~410-line method. It gathers state, computes the budget, scores the roster, builds every kind of action plan inline, then ranks, explores, and records the pending plan. ADR 0004 says a new shop behaviour is a new kind of plan, but adding one today means editing the middle of that method. Tests can only feed a whole state and see the one action that comes out, so a test of one plan kind's gate depends on every other kind losing.

The inline blocks, in the order they run today:

| # | Kind | Lines | Reads |
|---|---|---|---|
| 0 | Invisible dupe / sell-down | 634-641 | roster scores, ante, round, Invisible memory |
| 1 | Leave | 649-654 | budget |
| 2 | Joker buy, sell-then-buy swap | 659-763 | shop cards, run view, budget, slot counts, `slots_full`, roster scores |
| 3 | Roster order (Dagger feed or scoring order) | 767-798 | roster, roster scores, budget |
| 5 | Consumable buy | 801-826 | shop cards, consumable slots, raw state, strategy, budget |
| 6 | Pack buy | 829-858 | packs, roster, slots, budget |
| 7 | Voucher buy | 861-881 | vouchers, owned vouchers, budget |
| 8 | Campfire feed | 884-890 | consumables, run view, budget |
| 9 | Diet Cola sell | 893-907 | roster, shop cards, run view |
| 10 | Reroll | 910-931 | budget, rerolls this shop, slots, **the plans built so far** |
| 11 | Decay sells | 934-962 | roster scores, roster, slot counts |

## Facts the refactor must keep

- Ties go to the earlier plan: `max` keeps the first plan of equal value, and the decision record's sort is stable. Producer order is part of the behaviour.
- Reroll's base is 2.0 only when the best plan whose description contains "buy" is under 3.0 (`:913`). Those are exactly the joker, swap, consumable, pack and voucher plans.
- Invisible memory updates every evaluated tick: no Invisible → (None, False); first seen → (round, False); otherwise first-seen is kept and selling-down is True exactly when a sell-down plan was produced. The `fodder is None` branch (`:496`) can't be reached with 3+ jokers.
- Two slot counts are in use. The API `joker_count` feeds the budget, Riff-Raff's free-slot penalty and the Riff-Raff decay sell. `max(joker_count, len(owned))` feeds the direct-buy gate. Both stay.
- Campfire builds its own strategy without the deck (`:384`), not `run.strategy`.
- `score_consumable(key, state, strat)` takes the raw state.
- `test_shop_swap.py:200` and `test_one_joker_price.py:70` monkeypatch `shop_evaluator.joker_value`.

## Decisions

1. Glossary term **Shop view** (added). Code type `ShopView`, a frozen dataclass:
   - run view (`run_view(state)`)
   - budget
   - roster scores (`score_roster(run)`)
   - shop cards, packs, vouchers, owned vouchers
   - consumables held, consumable count and limit
   - joker count (API), joker limit, actual count, `slots_open`, `slots_full`
   - rerolls this shop, round number
   - Invisible memory (first seen, selling down) as it stood before this tick
   - the raw state, used only by the consumable producer (decision 8)
2. Each plan kind is a module-level producer `(view) -> list[ActionPlan]`. `_PRODUCERS` lists them in today's order: Invisible, leave, jokers, roster order, consumables, packs, vouchers, Campfire, Diet Cola, reroll, decay sells. `evaluate` concatenates their output. The tuple order settles ties, and a comment there says so. A new shop behaviour is a new producer plus one entry in `_PRODUCERS`.
3. Reroll is the one producer that reacts to the others: `(view, earlier) -> list[ActionPlan]`, where `earlier` is every plan produced before it. Decay sells still run after it.
4. `ActionPlan` gets `kind: str`, one of: `invisible_dupe`, `invisible_selldown`, `leave`, `joker`, `swap`, `reorder`, `dagger_feed`, `consumable`, `pack`, `voucher`, `campfire`, `diet_cola`, `reroll`, `decay_sell`. Reroll's "is a buy" test becomes `kind in {joker, swap, consumable, pack, voucher}`. The decision record and the payload are unchanged.
5. Evaluator memory: `slots_full` and the reroll count enter the view read-only. The Invisible producer is pure. After collecting, the evaluator updates Invisible memory from the view and the plans (rule above), so the Invisible logic exists once.
6. Everything stays in `shop_evaluator.py`, laid out as budget and item scorers → `ShopView` → producers → `ShopEvaluator`. Monkeypatch targets and knobs paths keep working.
7. Test surface: `ShopEvaluator.ranked_plans(state) -> list[ActionPlan]`, sorted by value with a stable sort, so `[0]` is the greedy pick. It has no side effects: no memory update, no exploration roll, no pending plan. `evaluate` stays the bot's one call: continue the pending plan, or build the view → collect → update memory → explore → emit (decision record, lean, `[SHOP]` log, pending plan, reroll and Diet Cola counter).
8. The view keeps the raw state for `score_consumable` only. Moving that onto the view is a follow-up.
9. Roster order is one producer, giving at most one plan, of kind `dagger_feed` or `reorder`.
10. `ShopEvaluator._view(state)` builds the view fresh on each evaluated tick. It isn't cached on the state, because it carries evaluator memory. The `joker_count` mismatch warning moves into `_view`, so it still logs once per tick at the same point.

## Steps

1. **Same play.** Add `ShopView`, `_view`, the producers, `kind`, `ranked_plans` and the Invisible memory update. `evaluate` becomes collect → rank → emit. Proof: record the 23 reference seeds (`scripts/trace/seeds.txt`) on the kernel (`BALATRO_KERNEL=rust`, simulator from `.venv-jackdaw/Scripts/python.exe sim/jackdaw_adapter/serve.py --port N`) for a HEAD export and the working tree. Actions must be identical with the PAYLOAD entries removed, and log lines identical, using a scratchpad filter in front of `compare.py`. `test_shop_swap`, `test_one_joker_price`, `test_decision_logging`, `test_money_opportunity_cost` and `test_exploration` must pass.
2. **Tests on plans.** Add `tests/test_shop_plans.py`, which asserts on `ranked_plans` per kind: a gate opens or closes, a kind is present or absent, two kinds rank against each other. It has one test per producer that lacks direct coverage today. Existing whole-action tests stay. Re-run the step 1 trace comparison as a sanity check.

After the build: the knobs shop reference's "Candidate plans" table names each producer, the `balatrobot` code-map entry describes view → producers → evaluator, and the knobs index says a new shop behaviour is a new producer plus an entry in `_PRODUCERS`. ADR 0004 is unchanged.

## Follow-ups (not in this work)

- Campfire prices off-strategy planets with a strategy built without the deck. Moving it to `run.strategy` changes play and needs its own paired measurement.
- `score_consumable` takes the raw state. Giving it the view (in `consumable_policy.py`) removes the view's raw-state field.
- The decision record could carry each plan's `kind`. That changes the payload, so the dashboard side would need a look.
