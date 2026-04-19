# Release notes

## v1.1.0 — 2026-04-19

Valuation refactor epic (#32). Eight phases folded the ad-hoc cascade of post-hoc floors, fallbacks, and patched-on adjustments in `evaluate_joker_value` into a single `SimContext` that carries everything the sim needs. Every new joker used to require its own patch layer; now it slots into a typed context that already knows about held cards, lifetime state, deck density, boss effects, and run-level observations.

### Headline changes

**`SimContext` consolidation.** Replaced the parameter cascade (`evaluate_joker_value` → `_scoring_delta` → `_synthetic_hand`) with one frozen dataclass carrying candidate, owned jokers, hand levels, strategy, ante, deck profile, held cards, lifetime state, live run stats, active boss, and MC sample count. All post-hoc floors and adjustments collapse into sim inputs.

**Held-in-hand phase sim.** Baron, Shoot the Moon, Raised Fist, Mime, and Blackboard now fire in the synthetic hand. Previously their value was estimated via bolted-on multipliers; now the sim sees the actual held cards and their effects apply naturally through the joker pipeline.

**Deck-density-aware synthetic hands.** Valuation now samples against the actual deck composition (rank / suit / enhancement density) rather than a vanilla 52-card assumption. Flush-build rosters get realistic flush-proc rates; Steel-heavy decks properly value Steel Joker.

**`LifetimeState` for scaling xmult.** Live "Currently X…" anchors parsed from owned joker effect text for Madness, Hologram, Canio, Vampire, Obelisk, Yorick, Campfire, Constellation, Throwback, Hit the Road, Lucky Cat, and Glass. A mid-run Hologram at X4.5 is now projected against its actual anchor, not X1.0.

**Economy joker ROI.** Unified dollars-to-value conversion (`DOLLARS_PER_VALUE_UNIT × ECO_ANTE_DECAY`) behind `utility_value.py`. Credit Card, Golden Ticket, Matador, Astronomer, Satellite, To Do List, Cloud 9, Delayed Gratification, Faceless, Gift Card, Business Card, and Rocket all share one ROI primitive.

**Event-generator ROI.** Thirteen new valuators cover DNA, Space, Vagabond, Cartomancer, Hallucination, Seance, Perkeo, Riff-Raff, Midas Mask, Certificate, Burnt, Merry Andy, Turtle Bean, and Marble — each projecting expected dollars over remaining rounds with joker-specific realization rates.

**Boss-blind-aware sim.** `BossBlindState` templates 13 bosses (Plant, Needle, Hook, Manacle, Pillar, Arm, Flint, Eye, Mouth, Head, Club, Window, Goad). In-round valuation uses the active boss; shop-phase valuation blends across the upcoming-boss pool weighted by `BOSS_WEIGHT`. Photograph collapses under Plant, Acrobat lifts under Needle, etc.

**Joker-order normalization.** `reorder_for_scoring` ports the live bot's `ReorderJokersForScoring` logic into the sim (chips → mult → xmult, Blueprint rightmost-compatible, Brainstorm leftmost). Hologram, Blueprint, and Brainstorm are no longer understated by owned-order scoring.

**Monte Carlo for stochastic jokers.** Misprint, Lucky Cat, Bloodstone, and Oops now route through an MC sample loop (default 16 samples) instead of expected-value approximations. Common random numbers — same seed for baseline and candidate — shrinks paired-difference variance.

### Live-run plumbing

- `LiveRunStats` observed by the bot each tick (`avg_discards_per_round`, `avg_sells_per_ante`, `avg_plays_per_round`) feed `LifetimeState`. Yorick and Campfire now project against actual per-run behavior instead of 1.5/1.5 defaults.
- Mid-round `blind_name` threads into `evaluate_joker_value` call sites (Hex selldown targeting, Buffoon pack picks) so boss-aware valuation fires outside the shop.

### Fixes

- `shop_events` sell records now populate `item_type` (`"joker"` / `"consumable"`). The filter in `_compute_live_stats` was silently returning 0 joker sells, overriding the 1.5 default with 0.0 for any sell-scaling projection.
- Retrigger jokers (Hanging Chad, Dusk, Sock and Buskin, Seltzer) now reach the scoring sim.
- Zero-value trap on face/rank-affinity jokers in shop valuation fixed.
- Blueprint/Brainstorm copy-incompatibility plumbed through valuation.

### Tooling

- Test count: 380 → 454
- Issue #32 epic closed (all 8 phase sub-issues shipped: #33, #34, #35, #36, #37, #38, #39, #40)

---

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
