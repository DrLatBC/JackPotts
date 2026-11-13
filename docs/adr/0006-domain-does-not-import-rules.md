# Decision logic lives in domain/, and domain/ never imports rules/

Rule classes in `rules/` only match a game state and hand off. All play, discard, shop, pack and targeting logic lives in `domain/`, and nothing in `domain/` or `joker_effects/` imports from `rules/`. Shared logic that two policies need goes into a `domain/` module, not into a helper beside the rules. The layers point one way so that circular imports can't creep back in, and so the decision logic can be tested without the rule engine.
