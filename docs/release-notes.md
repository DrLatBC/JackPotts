# Release notes

## v1.0.0 — 2026-04-18

First stable release. The codebase has been through a structural overhaul since `v0.2.0-beta`; enough subsystems were rewritten that the old rules engine is barely recognizable.

### Headline changes

**Unified shop evaluator.** Replaced 11 siloed shop rules with a single evaluator that scores the roster, computes a BUILD/FLEX/SPEND budget, enumerates every candidate action plan (buy, sell, reorder, reroll, pack, voucher, consumable, fodder move, campfire feed, Diet Cola sell, …), ranks by `net_value`, and emits the first step of the best plan. Plans can span multiple ticks; stateful sell-downs validate per tick.

**Unified discard ranking.** All discard decisions now flow through one Monte Carlo primitive — `_expected_play_value(keep_indices, ctx)`. It samples random deck draws, runs `best_hand()` with full joker effects, and averages. The old three-pass design ranked candidates with a joker-blind heuristic and buried high-value chases (Bloodstone + hearts, Triboulet + K/Q, Steel Joker + enhanced cards, The Idol target) before scoring ever saw them.

**The Idol mod patch.** Upstream balatrobot didn't expose the per-round idol target — ~99% of hands with The Idol had wrong predictions. Shipped a patch on our fork that adds `round.idol_card` to the gamestate serializer; threaded it through `Snapshot -> RoundContext -> ScoreContext` to apply ×2 mult per matching scored card.

**Typed data pipeline.** Raw API dicts now flow through `adapt_state()` into frozen `Snapshot` / `Card` / `Joker` / `HandLevel` / `DeckProfile` dataclasses. Eliminates a long tail of `.get(...) or []` bugs and makes the contract between adapter, context, and policy explicit.

**JackPotts dashboard integration.** New `dashboard_client.py` buffers per-game data and flushes gzip-compressed bulk uploads every 120s. Supervisor handles batch lifecycle (start / heartbeat / finish). Payload includes rich per-game arrays: actions, ante snapshots, hand scores, shop events, joker tracking. Live instance at [jackpotts.drlat.dev](https://jackpotts.drlat.dev).

**`CardProtection` consolidation.** Six separate parameters (`blackboard`, `rank_affinity`, `scoring_suit`, `strategy`, …) previously plumbed through every `cards_not_in` caller collapsed into one scored view built from `ctx.strategy`, jokers, boss state, and The Idol target.

**Scaling registry.** All scaling jokers now declare a `ScalingProfile` (trigger, gain, exploitation strategy) in `scaling.py`. Derived sets (`PLAY_SCALERS`, `DISCARD_SCALERS`, `FINAL_HAND_JOKERS`, `SELL_PROTECTED`, etc.) replace scattered hardcoded lists.

**Hand sequencing.** New round-level planner (`domain/policy/hand_sequencing.py`): milk early hands, set up Card Sharp, reserve Acrobat / Dusk for the finisher.

**Per-instance save profile isolation.** The supervisor launches each Balatro instance with its own save profile, so parallel runs don't clobber each other. Graceful shutdown added.

### Scoring & policy tweaks

- Raised chase margin from 1.3× to 1.4× play_ev
- Madness fodder gate is now one-way, nudges toward fodder buys
- Ceremonial Dagger positioning as a ranked shop plan
- Risk-adjusted Misprint mult; low-probability upside discounted
- Discard scarcity margin (chases must meaningfully beat play EV)
- Flush Pot / Four Fingers / Shortcut edge cases fixed
- Vampire + Flower Pot stripping fixed; order-dependent before-phase fixed
- `id()`-based card identity replaced with `is` checks
- Boss-specific scoring fixes: The Ox most-played tracking, The Wall milk margin, Verdant Leaf unlock, Crimson Heart discount, Cerulean Bell forced card, The Psychic 5-card min

### Tooling

- Test count: 266 → 380
- CI on Python 3.13; `balatrobot` moved to optional `[runtime]` extra
- Eager config validation with actionable error messages
- Integration harness: `setup_game_full`, `force_boss`, `advance_to_boss_select`, `inject_god_mode`, `burn_discards`, `cheat_win_if_needed`

### Bot split

The single `bot.py` has been decomposed — `run_bot()` is down from 642 lines to ~205 — with helpers split into `bot_logging.py` and `bot_format.py`. The rules layer uses `rules/_helpers.py` for shared padding and sort utilities.

---

## v0.2.0-beta and earlier

See [git history](https://github.com/DrLatBC/JackPotts/commits/master) for pre-1.0 changes.
