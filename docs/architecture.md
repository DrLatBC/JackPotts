# Architecture

## Big picture

```
Balatro (game) + Lovely mod loader + balatrobot mod
        |  JSON-RPC HTTP
balatrobot serve (uvx)
        |
bot.py --> engine.py --> rules/ --> domain/policy/
                                --> domain/scoring/
                                --> joker_effects/
        | (game results)
dashboard_client.py --> JackPotts API (optional)
```

## Decision flow (per tick)

```
API gamestate dict
  --> adapt_state()               # infrastructure/state_adapter.py -> Snapshot
  --> RoundContext.from_snapshot() # context.py -> RoundContext (cached per tick)
  --> engine.decide(state)        # engine.py -> iterates rules by priority
  --> Rule.match() / .action()    # rules/ -> returns Action dataclass
  --> action.to_rpc()             # actions.py -> JSON-RPC params
  --> client.call()               # sent to the balatrobot mod
```

Each game state (`SELECTING_HAND`, `SHOP`, `BLIND_SELECT`, `*_PACK`, `ROUND_EVAL`, …) has its own priority-ordered list of rules. The first rule whose `match()` returns true fires, and its `action()` is dispatched to the API.

## Module map

### Core loop

| File | Role |
|------|------|
| `src/balatro_bot/bot.py` | Game loop, polls state, dispatches actions, collects per-game data |
| `src/balatro_bot/engine.py` | Priority-ordered rule engine |
| `src/balatro_bot/context.py` | `RoundContext` — per-tick cached context with boss blind mutations; exposes `card_protection` |
| `src/balatro_bot/actions.py` | Action dataclasses + `Rule` protocol |
| `src/balatro_bot/strategy.py` | Hand/suit/rank/enhancement affinity from owned jokers; builds `CardProtection` |
| `src/balatro_bot/scaling.py` | Scaling joker registry, anti-synergy map, derived joker sets |

### Policy (`domain/policy/`)

| File | Role |
|------|------|
| `play_policy.py` | Winning play, high-value play, milk logic |
| `playing.py` | Playing-phase policy (joker sell logic, etc.) |
| `discard_policy.py` | Unified Monte Carlo discard ranking via `_expected_play_value` |
| `hand_sequencing.py` | Round-level plan: milk early, finisher on last hand |
| `shop_evaluator.py` | Unified shop evaluator: scores roster, budgets, enumerates action plans |
| `shop_valuation.py` | Joker valuation (scoring sim, synergy, context scaling, boss-aware) |
| `shop.py` | Shop helpers: deck profile, economy constants |
| `sim_context.py` | `SimContext`, `LifetimeState`, `LiveRunStats`, `BossBlindState` dataclasses |
| `utility_value.py` | ROI-based valuation for economy / event-generator jokers |
| `scaling_projection.py` | Future-value projections for scaling xmult / additive jokers |
| `boss_adjustment.py` | Per-joker boss-blind multipliers + shop-phase blended multiplier |
| `consumable_policy.py` | Consumable use decisions |
| `pack_policy.py` | Pack pick decisions (tarot, planet, buffoon, spectral) |
| `blind_policy.py` | Blind skip/select policy |

### Scoring (`domain/scoring/`)

| File | Role |
|------|------|
| `search.py` | `enumerate_hands`, `best_hand`, `discard_candidates` |
| `classify.py` | Hand classification + scoring-card extraction |
| `estimate.py` | Full scoring simulation |
| `draws.py` | Flush/straight draw probability |
| `chase.py` | Chase strategy generation for discards |
| `base.py` | Boss blind hand-level transforms (Flint, Arm, …) |

### Joker effects (`joker_effects/`)

| File | Role |
|------|------|
| `registry.py` | `apply_joker_effects(ctx)` — iterates jokers in order |
| `context.py` | `ScoreContext` (chips/mult/xmult), retrigger logic, optional RNG |
| `parsers.py` | Extracts chips/mult/xmult from joker effect text |
| `per_card.py` | Per-scored-card effect model (Fibonacci, Photograph, Triboulet, Idol, Bloodstone) |
| `scoring_phase.py` | Phase classification (chips/mult/xmult) + `reorder_for_scoring` |
| `simple.py` / `complex.py` | Individual joker effect functions |

### Data models (`domain/models/`)

| File | Role |
|------|------|
| `snapshot.py` | `Snapshot`, `BlindSnapshot`, `RoundSnapshot` (frozen dataclasses) |
| `card.py` | `Card`, `CardValue`, `CardModifier`, `CardState` |
| `joker.py` | `Joker`, `JokerAbility` |
| `hand_level.py` | `HandLevel` |
| `deck_profile.py` | `DeckProfile` (suit/rank/enhancement counts) |

### Infrastructure

| File | Role |
|------|------|
| `infrastructure/state_adapter.py` | Raw API dict -> typed `Snapshot` |
| `dashboard_client.py` | HTTP client for the JackPotts dashboard (optional) |
| `supervisor.py` | Multi-instance orchestrator with health monitoring |
| `config.py` | Paths, ports, env loading |

## Key design choices

### Unified discard ranking

All discard decisions flow through one Monte Carlo primitive: `_expected_play_value(keep_indices, ctx)` in `discard_policy.py`. For each candidate keep-set, it samples random draws from the deck, runs `best_hand()` with full joker effects on each hypothetical hand, and averages the totals. Hit rate and miss outcomes are captured implicitly by the sample distribution.

The previous three-pass design ranked candidates with a joker-blind heuristic, so candidates that would score huge with the current roster (Bloodstone + hearts, Triboulet + K/Q, Steel Joker + enhanced cards, The Idol's round target) got buried before ever reaching a joker-aware scoring step.

### Unified shop evaluator

Each shop tick, `ShopEvaluator` scores the roster (live EV delta per owned joker), computes an economy-phase budget (BUILD / FLEX / SPEND), enumerates every candidate action plan (buy, sell, reorder, reroll, pack, voucher, consumable, …), ranks them by `net_value = item_value × aggression − money_opportunity_cost`, and emits the first step of the best plan. Plans can span multiple ticks; stateful sell-downs (e.g. multi-joker Invisible dupe setup) validate per tick.

This replaces an older design with 11 siloed shop rules that couldn't trade off against each other.

### Unified joker valuation (`SimContext`)

`evaluate_joker_value(candidate, owned, …)` is the single entry point for pricing any joker — shop buys, shop sells, pack picks, Hex targeting. Inputs are bundled into a frozen `SimContext` (`domain/policy/sim_context.py`) that carries:

- Candidate joker, owned jokers, joker limit, strategy
- Hand levels, ante, deck profile (rank/suit/enhancement density)
- `LifetimeState` — live "Currently X…" anchors parsed from owned jokers for scaling xmult (Madness, Hologram, Canio, Vampire, Obelisk, Yorick, Campfire, Constellation, Throwback, Hit the Road, Lucky Cat, Glass)
- `LiveRunStats` — bot-observed per-run averages (`avg_discards_per_round`, `avg_sells_per_ante`, `avg_plays_per_round`) feeding scaling-joker projections
- `BossBlindState` — active boss template when known, else `None` (shop phase blends across the upcoming-boss pool)
- `monte_carlo_samples` — MC sample count for stochastic jokers (Misprint, Lucky Cat, Bloodstone, Oops)

Valuation runs in four layers:

1. **Scoring delta.** `_scoring_delta` builds a synthetic hand against the deck's actual density, normalizes joker order via `reorder_for_scoring` (chips → mult → xmult, Blueprint / Brainstorm placed adjacent to a compatible target), and scores with vs. without the candidate. Stochastic jokers route through an MC sample loop with common random numbers for variance reduction.
2. **Synergy multiplier.** Amplification pairs (Pareidolia × face-card jokers, Blueprint × strong neighbors, etc.), hand-type coherence, archetype bonuses.
3. **Context scaling.** Ante urgency, category diminishing returns.
4. **Boss adjustment.** In-round uses the active boss; shop phase uses `shop_blended_multiplier` weighted by `BOSS_WEIGHT` across the upcoming pool.

Economy / event-generator jokers (Golden Joker, Cartomancer, Space, Vagabond, Perkeo, …) are priced by ROI in `utility_value.py` — expected net dollars over remaining rounds × `DOLLARS_PER_VALUE_UNIT × ECO_ANTE_DECAY`.

This replaced an older cascade where every new joker required its own patch layer: per-card gates, projected-xmult floors, pivot floors, deck-composition adjustments, utility fallbacks. The refactor is documented in issue #32 (see [release notes](release-notes.md) for the v1.1.0 phase breakdown).

### `CardProtection`

`strategy.py` defines `CardProtection` — a frozen dataclass with `score(card) -> float`. Higher score = more valuable to keep. All `cards_not_in` callers pass `ctx.card_protection`, which combines signals from boss scoring suit, Blackboard suit constraint, The Idol's round target, joker rank/suit/enhancement affinity, debuff status, and raw rank tiebreaker.

Before the consolidation, six separate parameters were plumbed through every `cards_not_in` caller with no single reconciliation point.

### Boss blind handling

All boss-specific mutations live in `context.py` and the relevant scoring transforms live in `domain/scoring/base.py`. See [scoring.md](scoring.md) for the full list of handled bosses.
