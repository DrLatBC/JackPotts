# Scaling profile carries its projection

Status: done (2026-11-18). Architecture review item #10. `ScalingProfile.trigger_rate` holds the additive rates; the xmult projectors read `gain_per`; `LifetimeState.xmult_anchors` / `xmult_anchor(key)` replaces the 12 fields and the three anchor maps; Glass Joker's `gain_per` is 0.75; `SCALING_XMULT` is gone. `tests/test_scaling_projection.py` pins 126 projections (21 jokers × antes 1/4/8 × owned/fresh), recorded on HEAD before the move and passing unchanged after it, plus targeted tests. The 23 reference seeds on the kernel replayed with identical actions (4,012 without the 23 PAYLOAD entries; the payloads matched too) and logs (16,392 lines). `build_value_map()` output is byte-identical.
Deviation from decision 6: `project_end_state(ctx)` takes the SimContext alone, since it already carries the candidate and its key.

## Problem

A scaling joker's projection (see GLOSSARY.md) reads facts that are written more than once:

| Fact | Where it is written |
|---|---|
| Gain per trigger, xmult scalers | `ScalingProfile.gain_per` (`scaling.py`) and a literal in each `_project_*_gain` (`domain/policy/scaling_projection.py:42-161`) |
| Per-hand trigger rate, additive scalers | `_ADDITIVE_PROJECTORS` lambdas (`scaling_projection.py:220-230`), all constants |
| Joker → anchor field | `sim_context._XMULT_ANCHOR_FIELD`, `scaling_projection._XMULT_ANCHOR_ATTR`, an inline dict in `shop_valuation._joker_value` (`shop_valuation.py:2022`) |
| Anchors | 12 `*_xmult` fields on `LifetimeState`, one per joker |
| "Scaling xmult" set | `scaling.SCALING_XMULT` (profile xmult with gain > 0, includes Card Sharp and Acrobat; re-exported by `constants.py`, imported by nothing) and `scaling_projection.SCALING_XMULT_KEYS` (the 12 projected jokers) |

Ten of the twelve xmult literals match the profile. Glass Joker's profile says 0 and its projector says 0.75. Hit the Road's projector returns a flat 0.15 that is a per-hand average, not a gain. Wiring a new scaling xmult joker means editing four places (`knobs/references/valuation-sim.md` says so). `scaling_projection` has no direct tests.

## Game facts

Gains from `game.lua` (vanilla dump, `config.extra`): Constellation 0.1, Madness 0.5, Vampire 0.1, Hologram 0.25, Obelisk 0.2, Lucky Cat 0.25, Campfire 0.25, Throwback 0.25, Glass Joker 0.75 (`game.lua:494`), Hit the Road 0.5, Caino 1, Yorick xmult 1 per 23 discards. All match the registry except Glass Joker.

## Decisions

1. The profile carries data only. `ScalingProfile` keeps `trigger` and `gain_per` and gains `trigger_rate`: expected triggers per hand played (per discard for discard triggers), set for the nine additive scalers with today's values (Green Joker 1.0, Supernova 1.0, Ride the Bus 0.6, Square 0.3, Runner 0.15, Trousers 0.25, Hiker 5.0, Wee 0.2, Castle 0.5). `_ADDITIVE_PROJECTORS` is deleted. The twelve xmult models stay as per-joker functions in `scaling_projection` (they come in six shapes and read the run), each reading its gain from the profile instead of a literal. Realization discounts (the additive 0.5, Madness's 0.5) stay in the projection: they are valuation, not game facts.
2. Anchors are keyed by joker. `LifetimeState` replaces its 12 `*_xmult` fields with one mapping, read through `lifetime.xmult_anchor(key)` (X1.0 when not owned). `from_owned` fills it for every owned joker whose profile is xmult, from `current_value(joker, "xmult")` in `live_values` (ADR 0009), as now. The three anchor maps are deleted. The layer-3 candidate anchor in `_joker_value` reads the candidate's own current xmult directly instead of building a `LifetimeState`.
3. Glass Joker's `gain_per` becomes 0.75, and its projector reads it.
4. `scaling.SCALING_XMULT` and its `constants.py` re-export are deleted. `SCALING_XMULT_KEYS` (jokers with an xmult projector) is the one set of that name.
5. Hit the Road keeps 0.15 as a named per-hand-average constant in its projector, not tied to the gain.
6. The interface is `project_end_state(candidate, ctx)`, returning the `{xmult, chips, mult}` that `_joker_value` builds now (None for no contribution). It replaces `project_total_xmult` and `project_additive_total`; `_joker_value` makes one call.
7. Every float keeps today's operation order, so each projection returns bit-identical results.
8. Glossary term **Projection** (added).

## Steps

1. **Pin today's numbers.** On HEAD, add `tests/test_scaling_projection.py`: a characterization test over every projected joker at antes 1, 4 and 8, owned (with a non-trivial anchor) and unowned, through the current two functions; plus targeted tests for Yorick's counter, the Vampire / Lucky Cat / Glass densities, Constellation's planets used, Hologram's Marble/DNA enablers and the live-stats overrides. Record a HEAD trace of the 23 reference seeds and a HEAD value map.
2. **Move the facts.** Apply decisions 1-6. Point the characterization test at `project_end_state` with unchanged expected numbers. Update `test_joker_valuation.py` and `test_live_stats.py` anchor reads to `xmult_anchor`. Fix the stale "parsed from effect text" docstrings in the touched modules. Update `knobs/references/valuation-sim.md` (the anchor row, the projector table now reading gains and rates from the profile) and the `balatrobot` skill if it names the old maps.
3. **Prove no behaviour change.** With `BALATRO_KERNEL=rust` and a simulator (`.venv-jackdaw/Scripts/python.exe sim/jackdaw_adapter/serve.py --port N`), record the 23 reference seeds from a HEAD export and from the working tree with `scripts/trace/record.py`, and compare with `compare.py`, ignoring the one PAYLOAD entry per game: action lists and logs identical. `value_map.build_value_map()` before and after: identical. Targeted tests pass. Record the results here.
