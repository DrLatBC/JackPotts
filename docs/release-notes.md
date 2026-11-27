# Release notes

## Unreleased

- Big starts (`rl/starts.py`, from wardn's "big-starts" run): branch training keeps each game's copies at its last 9 build decisions (shop moves, pack picks, blind selects). A hand scoring 5 times its blind's target or more puts the oldest copy in a pool that keeps the best 200. Once the pool holds 128, a share of new training games start from a pool copy, each use making an entry 0.6 as likely to be drawn again. Restarted games keep their own name and are left out of the run's game counts, wins and mean ante; the pool is saved with the run. Train tab: STARTS (share, 0.25 for new runs) and POOL (200).
- Reward: every blind beaten or skipped earns a third of a point before the win as well as after (it was one point per boss), and OVERKILL (0.01 for new runs) adds that many points per natural log of a beaten blind's score over target, capped at 24. New runs default to win bonus 0 and endless 1, so every ante is worth the same.
- Shop branch chances lowered: synergy and voucher synergy 1.0 → 0.2, best joker from ante 6 0.5 → 0.2. In night6v2 these fired 1.1 to 1.9 times a game, the model's own move did better in most (synergy 5,429 to 2,859), and updates took 5 hours.
- Branch training no longer crashes when a run's hand size drops to zero or below: the shop branches' hand-score test deals empty hands there, and a lineup (as held or after a proposed buy) whose next blind draws no cards ends the run on the test's copy and scores 0, so no score-gated branch fires and no branch buys into it. night6 stopped on this 4.5 hours in, night7 at 14.5.
- Pack card branches: in a Standard pack, when the model passes over a Red Seal card, branch training tries taking it; when it takes a plain card while one with a seal, enhancement or edition is on offer, it tries a sealed card first, else an enhanced or edition one, at half of picks. From night5 u7's evals the model took enhanced cards at about the rate random picks would (47% vs 46%), Mult cards aside. Pack logs now name each playing card's seal and edition too (`H_7 m_bonus red_seal foil`).
- Copy-joker branches place what they buy: after buying Blueprint the branch moves it to the spot where the lineup scores best, since a bought joker lands on the right, where Blueprint copies nothing; after buying Brainstorm, which copies the leftmost joker, it moves the joker that scores best there to the left end. No buy branch sells a joker the rule bot never sells (`SELL_PROTECTED`: the scaling jokers, Blueprint and Brainstorm); a trace from night5 u7 had forced sales of Hologram, Constellation and similar costing 4 to 8 points each.
- Voucher and joker pairs: a shop voucher that feeds a held joker (Crystal Ball with Campfire and the card creators, Wasteful with the discard jokers, Seed Money with To the Moon, Observatory with Perkeo, Magic Trick with Hologram, Reroll Surplus with Flash Card, ...) gets a `voucher synergy` branch that redeems it, selling consumables to afford it but never a joker. The synergy branch also buys a joker that pairs with an owned voucher. Conflicts (Director's Cut or Retcon beside Chicot or Luchador, Wasteful beside Burglar, Petroglyph beside the discard scalers, Hieroglyph beside the per-hand scalers) get a conflict branch that skips the redeem or the joker. Tables: `VOUCHER_PAIRS` and `VOUCHER_CONFLICTS` in `scaling.py`, from the game's own voucher and joker text.

- Campfire branch: when the model is about to buy Campfire, branch training tries the shop visit without that buy and lets the model play on with its next pick, so the results decide what Campfire is worth instead of a rule. Tallied as `shop pass joker`.
- Madness conflicts: buying Madness while holding a joker it would cost you (one that stores value on itself, whether kept all run or reset on its own, plus Blueprint, Brainstorm and the legendaries) now gets a conflict branch that tries the shop visit without it. The list (`MADNESS_CONFLICTS` in `scaling.py`) came from checking every joker against the game's code; jokers whose value comes from the run (Fortune Teller, Throwback, Steel Joker, ...) or that decay are left out.
- More joker pairs: Merry Andy with the discard scalers (Castle, Yorick, Hit the Road) and Marble Joker with Stone Joker and Hologram (each Stone card Marble adds is a card added, X0.25 for Hologram per blind) join `AMPLIFICATION_PAIRS`, so the synergy branch tries them.
- Late joker swaps: from ante 6 the best-joker branch is tried at half of shop visits instead of a tenth, so the model gets practice replacing jokers that fall off late (Smiley Face and other flat +mult) with ones that raise the average hand score.
- Joker pair branches: when a joker on sale pairs with a held one (Pareidolia and face jokers, Smeared and suit jokers, Four Fingers, Shortcut, Splash, Oops, Ride the Bus; either way round), branch training tries buying it, never selling the partner to make room. When the model is about to buy a joker that works against a held one (Vampire with Glass Joker or Lucky Cat, Oops with Glass, Cavendish or Gros Michel, and the rest of the rule bot's conflict list), it tries the visit without that buy. Both come from the rule bot's own tables (`AMPLIFICATION_PAIRS`, `ANTI_SYNERGY`, now both in `scaling.py`). Tallied as `shop synergy` and `shop conflict`. The Midas Mask branch no longer sells Vampire to make room.
- Shop branches: branch training tries buys and uses the model under-makes, each once per shop visit at its own chance: Blueprint or Brainstorm, Midas Mask beside Vampire, the joker that raises the average best-hand score most (10% or more, selling a joker or consumables first if needed), a joker for an open slot it leaves the shop with while holding Madness (fodder for it to destroy), using an enhancing tarot held through a shop visit, and passing on a planet for a hand the run does not play. Every move of a branch's plan is trained at its own state with the branch's result. `decisions.jsonl` tallies each as `shop <kind>`.
- Boss-rule mask: under The Eye and The Mouth the model can no longer play a hand the boss scores 0 (a hand type already played, or any type but the round's first) while another play still scores. It applies in training, evaluations, final checks and the stream. On the 5,000 check seeds, night5 u7 wins 56.7% with it against 52.6% without (+4.0 ± 0.3 points).
- The model stream plays won games on into endless mode, and moves that only rearrange cards or jokers skip the delay.
- Learning runs can score past the win: with an endless bonus, won training games play on into endless mode and each endless blind beaten or skipped earns a third of the bonus. Both lines of a branch that win no longer tie by default. The Train tab sets it (ENDLESS) along with the step-size limit (CHANGE).
- The compiled Rust kernel is the only scorer. The Python scorer, the panel's KERNEL box, the supervisor's `--kernel` flag, `BALATRO_KERNEL` and PyPy simulator bots are gone; every bot runs on CPython with the kernel, which must be built first ([installation](installation.md)). Play is unchanged: batches already scored with the kernel.

## v2.0.0 — 2026-11-17

Jack Potts now has two players. The rule-based bot is joined by a model that started as a copy of it, then taught itself by playing the Jackdaw simulator. On 5,000 simulator seeds (Blue Deck, White Stake) it wins 33.8%, against about 14% for the bot over a 24,000-game batch. Around it: simulator batches at about 24,000 games an hour, a compiled Rust scoring kernel, and a desktop control panel for batches, learning runs and a live model stream.

### The model

- **Copy of the bot.** `rl.convert` turns simulator replays into examples and `rl.train` fits a transformer that picks the action type, its item and the set of hand cards. The first copy (`b187`, 24,000 bot games) wins 4.5% on the 5,000 evaluation seeds.
- **Learning by playing.** `rl.ppo train` plays Jackdaw and learns from the results. A game earns one point per boss beaten plus a win bonus. *Same-seed* runs play each seed four times and judge every game against the others. *Branch* runs copy the game at a decision, play another move on the copy, and learn the move that did better at that state. Runs pick their deck, stake and win bonus.
- **Progress, on the same 5,000 seeds:** 4.5% (copy) → 8.9% (after 1 hour) → 22.8% (a 9-hour run) → 33.8% (6 more hours), mean final ante 3.96 → 6.23.
- **Model stream.** A saved model plays the real, visible game through balatrobot with a shadow Jackdaw run checking every move. An OBS overlay shows what the model weighed before each move.

### Simulator and speed

- Batches can run on [Jackdaw](https://github.com/TylerFlar/jackdaw-balatro), a Rust reimplementation of the game, with an adapter that serves the fields the balatrobot fork adds. Balatro's own Lua runs headless as the exact cross-check.
- Simulator games are saved as compact replays (about 650 bytes a game); the dashboard gets their summaries.
- Hand search and scoring are about 4x faster in Python (card and lineup facts built once). A compiled Rust kernel (`kernel/`) gives the same answers about 85 times faster per search. Simulator bots run on PyPy when the kernel is off.
- Joker values are read from structured gamestate fields instead of description text.

### Control panel

`python -m balatro_bot.gui` (desktop shortcut "JackPotts"), a tray app that runs everything windowless:

- **Batches**: start, pause, resume and stop batches of up to 100 instances. Add or remove instances mid-batch with their own deck and stake. Pick the simulator and the kernel. Live per-instance detail and games per minute. Some instances can explore runner-up shop plans and pack picks.
- **Train**: start, stop, extend and compare learning runs. Each finished run is checked against its start, seed by seed.
- **Stream**: run the model stream.
- Batches run below normal priority and end with their supervisor, so the desktop stays usable and nothing is orphaned.

### Bot play

- Death keeps the better card (the right-hand one), in both the consumable and pack paths.
- Cerulean Bell's forced card is in every play and discard the bot chooses, including when Death copies it.
- Shop swaps run as one committed sell-then-buy plan; a queued buy never lands on full joker slots.
- Planets no longer build a hand the bot never plays.
- Scoring fixes: debuffed jokers score nothing in any phase, every Baseball Card multiplies, the Four Fingers flush suit follows the game, retrigger jokers and Blueprint/Brainstorm copy chains are counted in full, and held cards score left to right in large hands.
- Pack soft-locks are broken by a forced skip and a stall watchdog. A stuck simulator bot restarts after a minute.

### Dashboard and records

- Every shop and pack decision is logged with its scored options; final hands, blind antes and pack jokers are recorded.
- Failed game uploads are retried, 25 at a time.
- Learning runs and the model stream are sent to the dashboard.

### Project

- `GLOSSARY.md` and architecture decision records in `docs/adr/`.
- Tests: 454 → 842 (755 bot and panel, 87 for the model).

---

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

### Post-release valuation audit

Review turned up several jokers that were unmodeled or mis-modeled. All fixed under v1.1.0:

- **Caino typo**: codebase referenced `j_canio` everywhere (scaling registry, effect dispatch, lifetime anchor, projection); the real game key is `j_caino`. Caino was completely unmodeled — no anchor parsing, no effect fires, no projection floor. Now wired correctly.
- **Card Sharp**: fired on `played_this_round > 0` but the sim always built hand_levels with 0, so the X3 mult never triggered. Now synthesizes `played_this_round=1` during valuation and scales `raw_delta × 0.65` to account for the ~2-of-3.5 proc rate on repeat-type plays.
- **Acrobat**: the sim defaults `hands_left=1`, so Acrobat's finisher-only X3 always fired in the sim, overvaluing it 2-3×. Now scaled by `× 0.45` to reflect "fires on 1 of 3.5 hands, but the planned finisher."
- **Golden Ticket / Rough Gem / Chaos**: no ROI valuator. Added entries in `utility_value.py`.
- **Money threading**: `Bull`, `Bootstraps`, and `Banner` depend on `ScoreContext.money` / `discards_left`, which defaulted to 0 in the sim. Added `money` and `discards_left` fields to `SimContext`, threaded through `evaluate_joker_value` → `score_hand`, and wired `shop_evaluator` to pull from the live state.
- **Half Joker**: sim's synthetic hand pads every play to 5 cards; Half only fires at `played_cards ≤ 3`. Live bot already drops padding when Half is in the roster (`FEWER_CARDS_JOKERS`). Sim now mirrors that on High Card / Pair / Three of a Kind.

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
