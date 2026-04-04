# Project Assessment (Balatro Bot)

## Overall Impression

This is a strong project with real depth in game-specific logic. The codebase reflects practical learning from many edge cases, and the test inventory shows substantial effort toward correctness in a complex scoring domain.

If I had to summarize it in one line: **great domain intelligence, but architecture is currently the limiting factor for speed and reliability of future changes.**

## What’s Working Well

1. **Domain coverage is impressive**
   - There are many targeted tests and scenario-focused scripts around scoring and joker interactions.
   - That suggests the project has already captured lots of nuanced Balatro behavior.

2. **You already have useful seams**
   - `engine.py`, `rules/`, `strategy.py`, and `hand_evaluator.py` indicate there is a recognizable structure, even if coupling still leaks across boundaries.

3. **Pragmatic orientation**
   - The bot appears built from empirical iteration rather than overengineering, which is usually the right way for this type of project.

## Main Risks

1. **Raw dict coupling everywhere**
   - Shared mutable state shapes across modules increase regression risk and make “safe refactor” hard.

2. **God-module pressure**
   - `hand_evaluator.py` and rule modules likely accumulate unrelated responsibilities over time.

3. **Explainability debt**
   - When scoring + policy + orchestration are mixed, it becomes hard to answer “why did the bot choose this?” in a deterministic way.

4. **Test friction from runtime dependencies**
   - Some test paths require external dependencies (`balatrobot`) that may not always be present, slowing feedback loops.

## Recommended Priorities (Next 2–4 Weeks)

1. **Typed snapshot adapter first**
   - Introduce one canonical `GameSnapshot` model and adapter from API state.
   - Keep it read-only at first to avoid migration shock.

2. **Extract scoring into pure services**
   - Move classifier/base/joker score transforms behind stable function signatures.
   - Preserve old API as facades during transition.

3. **Policy objects over ad-hoc rule math**
   - Make rules orchestrate policy calls rather than compute values directly.

4. **Decision trace output**
   - Add a lightweight reason trace (`decision`, `top_alternatives`, `score_delta`) per action.
   - This pays huge debugging dividends.

5. **Split test suites by dependency**
   - Fast unit tests (pure domain) vs integration tests (client/runtime), so contributors can always run a reliable core suite.

## Maturity Snapshot

- **Domain Logic Quality:** High
- **Architecture Modularity:** Medium-Low (improving path is clear)
- **Test Intent:** High
- **Refactor Safety Today:** Medium-Low
- **Refactor Safety After separation plan:** High

## Bottom Line

You’re closer than it might feel: this looks like a project that can become exceptionally maintainable with a focused separation pass. The important part is preserving behavior while introducing boundaries incrementally—exactly the strategy outlined in the logic separation plan.
