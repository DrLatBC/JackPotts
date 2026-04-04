# Logic Separation Plan

## Problem Statement

The current codebase already has good building blocks (`engine.py`, `rules/`, `hand_evaluator.py`, `strategy.py`), but **decision flow, scoring mechanics, and game-state mutation are still coupled through shared raw API dicts**. This makes bug fixes and experiments hard because:

- rule evaluation and numeric estimation both read/write the same state shape,
- scoring utilities depend on Balatro API card/joker payloads directly,
- bot orchestration includes implementation details that belong to domain services.

## Desired Separation (Target Architecture)

Use a 4-layer split with one-way dependencies:

1. **Interface layer** (`bot.py`, `cli.py`, API client integration)
   - Owns process loop, retries, logging, and IO.
   - Converts external payloads into internal models.
2. **Application layer** (`engine.py`, high-level “use cases”)
   - Coordinates a turn/phase.
   - Calls domain services and emits actions.
3. **Domain layer** (new modules for scoring, hand analysis, strategy policies)
   - Pure game logic with typed inputs/outputs.
   - No HTTP, no logging side-effects, no direct API payload assumptions.
4. **Infrastructure layer** (adapters/serializers)
   - Mapping between Balatro API dicts and internal models.

Rule of thumb: **if logic can be unit-tested without a live client or logging setup, it belongs in domain/application, not interface.**

## Proposed Module Boundaries

### 1) `domain/models.py` (new)

Introduce typed dataclasses (or `TypedDict` if preferred for gradual migration):

- `CardModel`
- `JokerModel`
- `HandContext` (played, held, hand levels, blind, money, deck stats)
- `RoundState` (hands/discards/chips/goal)
- `ShopState` (shop offers, cash, joker slots)

Purpose: remove “stringly-typed” cross-module contracts.

### 2) `domain/scoring/` (new)

Move pure scoring and hand math from `hand_evaluator.py` plus joker score modifiers from `joker_scoring_phase.py` into:

- `classify.py` (hand detection)
- `base_score.py` (chips/mult base calculations)
- `joker_modifiers.py` (joker contribution functions)
- `estimate.py` (deterministic + EV wrappers)

Public API example:

```python
estimate_hand_score(ctx: HandContext) -> ScoreBreakdown
```

### 3) `domain/strategy/` (new)

Extract candidate generation / comparison logic currently spread across `strategy.py`, `rules/playing.py`, and parts of `rules/shop.py`:

- `play_policy.py` (which hand to play)
- `discard_policy.py`
- `shop_policy.py`
- `consumable_policy.py`

Each policy returns a **decision object** (not final API action):

```python
PlayDecision(cards_to_play=[...], rationale="...")
```

### 4) `application/planner.py` (new)

Single orchestrator per phase:

- Input: `GameSnapshot` (typed)
- Calls strategy + scoring services
- Output: `Intent` (`PLAY`, `DISCARD`, `BUY`, `REROLL`, etc.)

The existing `RuleEngine` can remain, but rules should depend on planner/domain interfaces, not raw dict internals.

### 5) `infrastructure/adapters/` (new)

- `state_adapter.py`: API dict -> typed snapshot
- `action_adapter.py`: intent -> API action payload

All schema drift handling and missing-field defaults live here.

## Migration Plan (Low-Risk, Incremental)

### Phase 0: Safety Net

- Add characterization tests around current scoring outputs and key decisions.
- Freeze these as compatibility tests before moving logic.

### Phase 1: Typed Read Models

- Introduce `domain/models.py`.
- Add adapters that build models from current state dicts.
- Keep old code paths, but let rules read through model wrappers first.

### Phase 2: Scoring Extraction

- Move pure scoring functions into `domain/scoring/`.
- Keep legacy `hand_evaluator.py` as a thin facade that forwards to new module.
- Verify parity with existing test suite.

### Phase 3: Strategy Extraction

- Move play/discard/shop heuristics into `domain/strategy/` policies.
- Replace direct rule math with policy calls.
- Preserve `RuleEngine` priorities to avoid behavior drift.

### Phase 4: Intent-Based Execution

- Add planner + intent/action adapter.
- Rules return intents or select policy outputs.
- `bot.py` only executes adapted actions.

### Phase 5: Cleanup

- Remove dead helpers duplicated between rules and evaluator.
- Tighten type hints and enforce no raw dict access in domain modules.

## Immediate Refactor Targets (High ROI)

1. **`bot.py` scoring log path**: pull `_log_played_hand` calculations into a reusable score-report service so bot loop is orchestration-only.
2. **`rules/playing.py`**: replace direct evaluator calls with `play_policy` interface.
3. **`rules/shop.py` + `joker_valuation.py`**: define a single `ShopDecisionContext` and central value function.
4. **`hand_evaluator.py`**: split into classify/evaluate/draw-prob modules to reduce “god file” pressure.

## Dependency Contract Checklist

When done, enforce these constraints:

- `domain/*` imports only stdlib + domain modules.
- `application/*` may import domain but not API client.
- `infrastructure/*` may import API client and domain.
- `bot.py` imports application + infrastructure only.

## Definition of Done

You can call this separation successful when:

- scoring changes can be made in one place without touching rules,
- rules choose actions using typed contexts (no deep dict spelunking),
- bot loop no longer contains hand-evaluation or valuation details,
- at least 80% of domain modules are unit-tested without API mocks.
