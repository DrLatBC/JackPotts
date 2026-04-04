# Logic Separation Plan

## Goal

Separate the bot into clear layers so we can change scoring, policy, and runtime behavior independently — and so new contributors can orient quickly and submit focused PRs.

The project already has useful structure, but the current center of gravity is still a handful of large modules that each mix multiple responsibilities:

- `context.py` is both an API adapter and a cached domain summary.
- `hand_evaluator.py` owns hand classification, score estimation, candidate enumeration, and discard EV.
- `rules/playing.py` and `rules/shop.py` hold both orchestration and policy math.
- `joker_valuation.py` is conceptually a shop-domain service, but it still builds raw synthetic card dicts and reaches directly into evaluator internals.
- `bot.py` still contains scoring-analysis logic in `_log_played_hand`, which is application/domain work living in the runtime loop.

The main problem is not that the code is messy. It is that the same concepts are represented in different places at different abstraction levels, so one rules tweak can force changes across evaluation, logging, and action code.

## Current Pressure Points

### 1. Raw state dicts leak everywhere

Today most modules consume the live Balatro API payload shape directly. That means:

- domain logic depends on string keys and missing-field fallbacks,
- boss-blind normalization happens inside context creation instead of at the boundary,
- test fixtures have to mimic API payloads even for pure logic.

### 2. Read models and decision models are fused

`RoundContext` is doing three jobs:

- adapting the external state,
- computing reusable derived facts,
- caching the best-hand decision input/output.

That makes it convenient, but it also makes it the place where unrelated responsibilities accumulate.

### 3. Policy and mechanics are interleaved

Examples:

- `MilkScalingJokers` contains both tactical rule gating and detailed hand/discard generation.
- `BuyJokersInShop` depends on valuation logic scattered across constants, parser output, and `evaluate_joker_value()`.
- `bot._log_played_hand()` re-derives scoring concepts that should come from a reusable score-report service.

### 4. One file still represents too many concepts

`hand_evaluator.py` (~15K lines) is the biggest architectural choke point. It currently covers:

- hand classification,
- draw quality and hit probability,
- base scoring,
- joker scoring phases,
- scoring-card extraction,
- hand enumeration,
- best-hand selection,
- discard candidate generation.

Those are related, but they are not the same layer.

## Target Architecture

Use a 4-layer split with one-way dependencies:

1. Interface
   - Runtime loop, CLI, logging, external client.
   - Files: `bot.py`, `cli.py`, `supervisor.py`.
2. Application
   - Phase orchestration, rule ordering, intent selection, decision traces.
   - Files: `engine.py`, `rules/`.
3. Domain
   - Pure game logic: models, scoring, strategy, valuation, policy helpers.
   - No API client imports, no logging side-effects.
4. Infrastructure
   - API-state adapters and action serialization.
   - Knows the external schema so the domain does not have to.

Rule of thumb:

- If code answers "what is true about this game state?" or "what is the best choice?", it belongs in domain/application.
- If code answers "how do I talk to balatrobot?" or "how do I log this?", it belongs in interface/infrastructure.

## Conventions

These apply to all new code introduced during the refactor:

- **Data types**: use `@dataclass(frozen=True)` for models, `NamedTuple` for lightweight return tuples. No `TypedDict` for internal models.
- **Imports**: absolute imports throughout (`from balatro_bot.domain.scoring.classify import ...`). No relative imports.
- **Naming**: modules are `snake_case`, classes are `PascalCase`, module-level constants are `UPPER_SNAKE`.
- **No re-exports from `__init__.py`** in new packages except where needed for a public facade.

## Concrete Target Layout

The current layout is `src/balatro_bot/` with flat modules. The target adds nested packages under that root. The package restructure happens incrementally — each phase creates its target directories as needed. No big-bang move.

### `src/balatro_bot/domain/models/`

Purpose: replace stringly-typed cross-module contracts with internal read models.

Modules:

- `cards.py` — `CardModel`, `JokerModel`
- `state.py` — `GameSnapshot`, `RoundState`, `BlindState`, `EconomyState`
- `decisions.py` — `HandSearchResult`, `DiscardOption`, `ScoreBreakdown`

Core types with key fields:

```python
@dataclass(frozen=True)
class CardModel:
    rank: str | None        # "2"-"9", "T", "J", "Q", "K", "A" (None for Stone)
    suit: str | None        # "H", "D", "C", "S" (None for Stone)
    enhancement: str | None # "WILD", "STONE", "BONUS", "GOLD", "STEEL", etc.
    edition: str | None     # "POLYCHROME", "HOLO", "FOIL", None
    seal: str | None
    debuffed: bool
    index: int              # position in hand/deck for action references

@dataclass(frozen=True)
class JokerModel:
    key: str                # e.g. "j_joker"
    label: str
    rarity: int             # 1=Common, 2=Uncommon, 3=Rare, 4=Legendary
    edition: str | None
    effect_text: str
    parsed_value: float | None  # extracted from effect text
    cost_sell: int
    index: int              # position in joker slots

@dataclass(frozen=True)
class BlindState:
    key: str
    name: str
    score: int
    is_boss: bool

@dataclass(frozen=True)
class RoundState:
    chips_scored: int
    hands_left: int
    discards_left: int

@dataclass(frozen=True)
class EconomyState:
    money: int
    joker_count: int
    joker_limit: int

@dataclass(frozen=True)
class GameSnapshot:
    state_name: str         # "SELECTING_HAND", "SHOP", etc.
    seed: str
    ante: int
    round_num: int
    economy: EconomyState
    round: RoundState
    current_blind: BlindState
    hand_cards: list[CardModel]
    deck_cards: list[CardModel]
    jokers: list[JokerModel]
    hand_levels: dict[str, dict]  # {hand_type: {chips, mult, level}}
    consumables: list[dict]       # raw for now, typed later
    shop: dict | None             # raw for now, typed later
```

Important distinction:

- `GameSnapshot` represents adapted external state — "what does the API say?"
- `RoundFacts` (computed later from snapshot) represents derived truths — "what does this mean for decisions?"

That split prevents the current `RoundContext` problem where source data and interpretation live in one mutable object.

### `src/balatro_bot/domain/scoring/`

Purpose: isolate scoring mechanics from play policy.

Modules:

- `classify.py` — hand classification, scoring-card extraction
- `base.py` — base hand chips/mult from hand level data
- `cards.py` — per-card chips, modifiers, debuff handling
- `jokers.py` — joker contribution pipeline
- `estimate.py` — `score_hand`, `score_hand_detailed`, score breakdown objects
- `search.py` — hand enumeration and best-hand selection
- `draws.py` — flush/straight/two-pair/trips draw quality helpers

Current file mapping:

- `hand_evaluator.classify_hand` -> `domain/scoring/classify.py`
- `hand_evaluator._scoring_cards_for` -> `domain/scoring/classify.py`
- `hand_evaluator.score_hand*` -> `domain/scoring/estimate.py`
- `hand_evaluator.enumerate_hands` and `best_hand` -> `domain/scoring/search.py`
- `hand_evaluator.*draw_quality` -> `domain/scoring/draws.py`
- `joker_scoring_phase.py` -> fold into `domain/scoring/jokers.py` or delete after parity
- `joker_effects/*` can remain a subpackage, but it should become an implementation detail of domain scoring, not an alternate scoring entry point

Public API should be intentionally small:

```python
class ScoreBreakdown: ...
class HandSearchResult: ...

def score_play(request: ScoreRequest) -> ScoreBreakdown: ...
def find_best_play(request: SearchRequest) -> HandSearchResult | None: ...
def rank_discards(request: DiscardSearchRequest) -> list[DiscardOption]: ...
```

### `src/balatro_bot/domain/strategy/`

Purpose: hold long-lived build identity, not immediate action selection.

Modules:

- `affinity.py` — affinity tables (hand, suit, rank, enhancement)
- `archetypes.py` — `ArchetypeProfile`, `ARCHETYPE_REGISTRY`
- `profile.py` — `Strategy`, `compute_strategy()`

What belongs here:

- `Strategy`
- `ArchetypeProfile`
- affinity tables
- `compute_strategy()`

What does not belong here:

- shop buy/sell thresholds,
- specific discard actions,
- play-now tactical rules.

Those are policy decisions that consume strategy, not strategy itself.

### `src/balatro_bot/domain/policy/`

Purpose: convert facts + strategy + scoring into candidate decisions. These are the reusable decision functions that rules delegate to.

Modules:

- `play_policy.py` — winning hand selection, high-value fallback, milk-scaler lines
- `discard_policy.py` — discard generation, ranking, boss-aware constraints
- `shop_policy.py` — joker buy/sell comparisons, interest/slot pressure, valuation
- `consumable_policy.py` — target selection and use gating
- `pack_policy.py` — pack pick scoring
- `blind_policy.py` — skip/select evaluation

The rules in `rules/` become thin wrappers:

```python
# rules/playing.py
class PlayWinningHand(Rule):
    def evaluate(self, state):
        ctx = RoundContext.from_state(state)
        decision = play_policy.choose_winning_play(ctx)
        if decision:
            return PlayCards(decision.card_indices, reason="winning hand")
        return None
```

A rule's job is gating (should this policy even run?) and action serialization (turn the decision into an RPC-ready action). The policy's job is the actual decision math.

### `src/balatro_bot/application/`

Purpose: keep phase orchestration and rule ordering, but stop doing domain work directly.

Modules:

- `score_reporting.py` — extracted from `bot._log_played_hand()`

The existing `engine.py` and `rules/` package stay at their current paths — they are already application-layer code. No move needed.

### `src/balatro_bot/infrastructure/`

Purpose: own all API-shape translation and serialization.

Modules:

- `state_adapter.py` — `adapt_state(raw_dict) -> GameSnapshot`
- `action_adapter.py` — serialize decisions into RPC format (if `actions.py:to_rpc()` outgrows its current home)

Responsibilities:

- normalize empty-list-vs-dict oddities (`modifier` and `state` can be `[]` instead of `{}`),
- convert current blind payload into a consistent internal `BlindState`,
- compute safe defaults for absent fields,
- parse joker effect text via `parsers.py` during adaptation.

## What Stays Where

### Keep in interface

- logging setup, retry loops, server health waits, runtime session tracking, CLI parsing

So:

- `bot.py` keeps `setup_logging()`, `wait_for_server()`, and the main loop
- `_log_played_hand()` moves to `application/score_reporting.py`

### Keep in application

- game-state-to-phase dispatch, rule ordering, first-matching-rule-wins behavior

So:

- `engine.py` stays at its current path
- `rules/` stays at its current path
- rules gradually thin out as policy functions are extracted

### Keep in domain

- poker rules, joker effects, strategy inference, valuation, synthetic scoring comparisons, boss blind mechanical adjustments

### Keep in infrastructure

- Balatro API schema quirks, external payload parsing, action serialization

## File-by-File Refactor Map

### `context.py`

Current roles:

- adapts API state
- applies boss-specific hand-level normalization
- computes strategy
- computes best hand
- caches results on raw state

Target split:

- adapter pieces -> `infrastructure/state_adapter.py`
- pure boss transforms -> `domain/scoring/base.py`
- strategy computation -> `domain/strategy/profile.py`
- best-hand search -> `domain/scoring/search.py`
- derived summary object -> stays as `context.py` but slimmed down

Recommended end state:

```python
snapshot = adapt_state(state)
ctx = RoundContext.from_snapshot(snapshot)
```

`RoundContext` survives as a slim derived-facts object. It stops doing adaptation, stops caching on the raw state dict, and delegates boss transforms to domain helpers.

### `hand_evaluator.py`

This becomes a compatibility facade during migration.

Recommended intermediate state:

- keep all public names (`classify_hand`, `best_hand`, `score_hand`, etc.),
- forward each function to its new home in `domain/scoring/`,
- mark the file as legacy facade with a module docstring.

End state: either delete after all callers migrate, or keep as a thin re-export module for convenience.

### `strategy.py`

Already close to a good domain module.

Needed cleanup:

- keep only build inference and affinity/archetype definitions,
- move tactical consumers out,
- becomes `domain/strategy/` with `affinity.py`, `archetypes.py`, `profile.py`

This file should become the anchor for "what build are we?".

### `joker_valuation.py`

Target home: domain policy support for shop decisions.

Recommended split:

- synthetic sample-hand construction -> `domain/scoring/samples.py`
- static category tables -> `domain/policy/shop_constants.py`
- value calculation -> `domain/policy/shop_valuation.py`

Why:

- valuation is not pure scoring,
- but it is not a shop rule either,
- it is a reusable policy service consumed by shop and pack selection.

### `rules/playing.py`

Target end state:

- rules remain as ordered gates,
- detailed action generation moves into `domain/policy/play_policy.py` and `discard_policy.py`

Good first extraction targets:

1. best-play-now selection
2. discard improvement ranking
3. milk-scaler action search

`MilkScalingJokers` is the biggest win here because it currently mixes gating, optimization, and card ordering.

### `rules/shop.py`

Target end state:

- rule ordering stays here,
- ranking/value logic moves out

Good first extraction targets:

1. shared shop facts object (slot pressure, interest pressure, effective budget)
2. buy/sell value comparator via `shop_valuation.py`

The immediate architectural smell here is duplicated policy state:

- some value logic is hardcoded in shop rules,
- some lives in `joker_valuation.py`,
- some lives in helper constants,
- some comes from parser heuristics.

That should collapse into one shop valuation service plus one shop facts object.

### `bot.py`

Target end state:

- orchestrates polling, transition detection, RPC execution, logging
- does not classify hands or compute detailed score breakdowns directly

Extract:

- `_log_played_hand()` -> `application/score_reporting.py`

That service can consume typed snapshots and scoring breakdowns, then return structured log lines or a report object.

## Recommended Migration Sequence

The safest path is to separate read models first, then mechanics, then policy.

### Phase 0: Freeze current behavior ✓ DONE

Added 17 characterization tests in `tests/test_characterization.py` covering:

- `best_hand()` — 5 tests (simple pair, flush beats pair, pair with joker, Psychic min_cards, Mouth locked hand)
- `score_hand()` — 4 tests (basic pair, pair+joker, pair+duo xmult, leveled pair)
- `discard_candidates()` — 2 tests (keep pair discard junk, all-junk keeps highest)
- `evaluate_joker_value()` — 3 tests (utility joker, scoring joker, xmult>flat)
- `RoundContext.from_state()` — 3 tests (normal blind, Psychic min_cards=5, Flint halves levels)

**Test approach for all phases:** The Phase 0 characterization tests stay green throughout the entire refactor. Every phase is a no-op from the test suite's perspective — same inputs, same outputs. If a phase requires new internal modules, add unit tests for those modules in that phase, but never remove or weaken a characterization test.

### Phase 1: Introduce typed snapshots without changing behavior ✓ DONE

Created:

- `domain/models/snapshot.py` — `Snapshot`, `RoundSnapshot`, `BlindSnapshot` (frozen dataclasses)
- `domain/models/__init__.py` — re-exports
- `infrastructure/state_adapter.py` — `adapt_state(raw_dict) -> Snapshot`
- `context.py` — added `RoundContext.from_snapshot()` staticmethod
- `domain/policy/playing.py` — `choose_verdant_leaf_unlock()`, `choose_sell_luchador()` extracted from rules
- `domain/policy/shop.py` — 9 `choose_*()` functions extracted from rules + constants/helpers
- `rules/playing.py` — `VerdantLeafUnlock` and `SellLuchador` delegate to policy
- `rules/shop.py` — all 9 shop rules delegate to policy

Tests added: `test_state_adapter.py` (2), `test_playing_policy.py` (4), `test_shop_policy.py` (18). All 178 tests pass.

### Phase 2: Split `hand_evaluator.py` ✓ DONE

Split `hand_evaluator.py` (1293 lines) into 4 focused modules under `domain/scoring/`:

- `domain/scoring/classify.py` — `classify_hand()`, `_scoring_cards_for()`, `_rank_counts()`, `_is_flush()`, `_is_straight()`
- `domain/scoring/estimate.py` — `score_hand()`, `score_hand_detailed()`, `_apply_before_phase()`, `_apply_card_scoring()`
- `domain/scoring/draws.py` — `flush_draw()`, `straight_draw()`, probability helpers, all 4 `*_draw_quality()` functions
- `domain/scoring/search.py` — `HandCandidate`, `ChaseCandidate`, `enumerate_hands()`, `best_hand()`, `cards_not_in()`, `discard_candidates()`

`hand_evaluator.py` is now a thin forwarding facade (~50 lines of re-exports). All 178 tests pass unchanged.

Do this before touching `RoundContext` or rule behavior — the evaluator has no upstream dependencies on context shape, so it can be split cleanly in isolation.

Move in this order:

1. `classify_hand()` and `_scoring_cards_for()` -> `domain/scoring/classify.py`
2. `score_hand()` and `score_hand_detailed()` -> `domain/scoring/estimate.py`
3. card chip helpers -> `domain/scoring/cards.py`
4. draw-quality helpers -> `domain/scoring/draws.py`
5. `enumerate_hands()` and `best_hand()` -> `domain/scoring/search.py`
6. `discard_candidates()` -> `domain/scoring/search.py` (or `draws.py`)

After each step:

- `hand_evaluator.py` keeps a forwarding import (`from domain.scoring.classify import classify_hand`)
- all existing callers continue to work unchanged
- add unit tests for each new module as it's created

Why this order:

- classification and scoring are leaf functions with no internal dependencies,
- draw quality depends on classification,
- search depends on scoring + classification,
- the migration risk stays controlled at each step.

### Phase 3: Replace `RoundContext` with layered facts ✓ DONE

Now that scoring is split, `RoundContext` can be slimmed down without touching evaluator internals.

Create:

- `RoundContext.from_snapshot(snapshot)` alongside existing `from_state(state)`
- both produce identical `RoundContext` objects (tested)
- `from_state` becomes `adapt_state(state)` + `from_snapshot(snapshot)` internally

Move into domain helpers:

- `flint_halve_hand_levels()` -> `domain/scoring/base.py`
- `arm_reduce_hand_levels()` -> `domain/scoring/base.py`
- boss-blind field extraction -> consumed from `GameSnapshot.current_blind`

Success condition:

- `RoundContext` no longer touches the raw state dict,
- `from_state()` still works as a convenience shim (calls adapt + from_snapshot),
- no domain module needs to mutate or cache data on the raw state dict.

### Phase 4: Extract shop valuation ✓ DONE

Created:

- `domain/policy/shop_valuation.py` — `evaluate_joker_value()` and supporting helpers
- `domain/policy/shop_facts.py` — `ShopFacts` (slot pressure, interest floor, effective budget)

Moved all valuation logic (synthetic hand generation, utility weights, synergy multipliers,
context scaling, `evaluate_joker_value()`) from `joker_valuation.py` into
`domain/policy/shop_valuation.py`. Created `domain/policy/shop_facts.py` with `ShopFacts`
dataclass (slot pressure, interest floor, effective budget).

Updated direct callers to import from the new canonical location:
- `domain/policy/shop.py` — `evaluate_joker_value`
- `rules/shop.py` — `evaluate_joker_value`, `UTILITY_VALUE`
- `rules/consumables.py` — `evaluate_joker_value`
- `rules/_helpers.py` — `evaluate_joker_value` (lazy import)
- `rules/packs.py` — `evaluate_joker_value` (lazy import)

`joker_valuation.py` is now a re-export facade (same pattern as `hand_evaluator.py`).
Test files continue to import via the facade. All 178 tests pass.

### Phase 5: Extract play and discard policies ✓ DONE

Created:

- `domain/policy/play_policy.py` — `choose_winning_play()`, `choose_high_value_play()`,
  `choose_best_available()`, `choose_milk_play()` and milk helpers (`_milk_discard`,
  `_milk_play_action`)
- `domain/policy/discard_policy.py` — `choose_discard()` and EV helpers (`_sample_miss_ev`,
  `_chase_ev`, `_best_chase`)

All five playing rules now delegate to policy functions:
1. `PlayWinningHand` → `play_policy.choose_winning_play(ctx)`
2. `PlayHighValueHand` → `play_policy.choose_high_value_play(ctx)`
3. `PlayBestAvailable` → `play_policy.choose_best_available(ctx)`
4. `DiscardToImprove` → `discard_policy.choose_discard(ctx)`
5. `MilkScalingJokers` → `play_policy.choose_milk_play(ctx)`

`rules/playing.py` is now a thin wrapper — each rule class's `evaluate()` builds
a `RoundContext` and calls the corresponding policy function. `DiscardToImprove`
retains backward-compatible static/classmethods that forward to `discard_policy`
for existing test compatibility.

Tests added: `test_play_discard_policy.py` (20 tests). All 198 tests pass.

### Phase 6: Extract consumable, pack, and blind policies ✓ DONE

Created:

- `domain/policy/consumable_policy.py` — `score_consumable()`, `_score_targeting_tarot()`,
  `evaluate_hex()`, `score_use_now()`, `score_hold()`, `eval_suit_convert()`, `eval_glass()`,
  `eval_enhancement()` extracted from `rules/_helpers.py` and `UseConsumables` methods
- `domain/policy/pack_policy.py` — `score_planet_card()`, `choose_from_planet_pack()`,
  `choose_from_buffoon_pack()`, `score_spectral_card()`, `choose_from_spectral_pack()`,
  `choose_from_tarot_pack()` with `HAND_VALUE` and `SPECTRAL_SCORES` constants
- `domain/policy/blind_policy.py` — `choose_skip_for_tag()` stub (returns False)

All six rule files now delegate decision math to policy:
1. `rules/consumables.py` — `UseConsumables` keeps state machine (hex selldown,
   staleness tracking, slot pressure), delegates scoring to `consumable_policy`
2. `rules/packs.py` — all 5 pack rules delegate to `pack_policy` functions
3. `rules/blind.py` — `SkipForTag` delegates to `blind_policy.choose_skip_for_tag()`
4. `rules/_helpers.py` — `score_consumable` and `evaluate_hex` are now re-exports
   from `consumable_policy`; targeting/card-selection helpers remain in `_helpers.py`

Tests added: `test_consumable_pack_policy.py` (35 tests). All 233 tests pass.

### Phase 7: Clean up the compatibility layer

Once the new modules are stable:

- reduce `hand_evaluator.py` to a re-export shim or remove it entirely,
- reduce `joker_valuation.py` to a re-export shim or remove it,
- reduce `context.py:from_state()` to a two-line shim,
- collapse duplicated constants,
- move `_log_played_hand()` -> `application/score_reporting.py`,
- enforce dependency boundaries (can be a CI lint rule or just documented).

## Dependency Rules

When the separation is healthy, these should be true:

- `domain/*` imports only stdlib plus other `domain/*`
- `application/*` (including `engine.py`, `rules/`) may import `domain/*` and `infrastructure/*`
- `infrastructure/*` may import `domain/models/*` but not `application/*`
- `bot.py` imports `application/*`, `infrastructure/*`, and runtime libraries
- `rules/*` do not call scoring internals directly — they call policy functions

## Definition of Done

This separation is successful when all of the following are true:

- scoring mechanics can change without editing shop or play rules,
- API schema quirks are handled at the adapter boundary,
- `bot.py` is orchestration-only,
- `RoundContext` no longer acts as the universal object passed everywhere,
- joker valuation has one canonical service,
- play and discard rules mostly coordinate policy results instead of implementing them,
- most pure logic can be tested without `BalatroClient`, logging, or raw API payload fixtures,
- a new contributor can find the right file to edit by looking at the package name.
