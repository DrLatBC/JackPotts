from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.actions import Action
from balatro_bot.context import RoundContext

if TYPE_CHECKING:
    from typing import Any


class VerdantLeafUnlock:
    """Sell weakest joker to lift Verdant Leaf's full-debuff on all cards.

    Verdant Leaf debuffs every playing card until one joker is sold.
    Fires immediately on the first SELECTING_HAND tick — sell the cheapest
    non-scaling joker, then normal play resumes.
    """
    name = "verdant_leaf_unlock"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.playing import choose_verdant_leaf_unlock
        ctx = RoundContext.from_state(state)
        return choose_verdant_leaf_unlock(ctx)


class MilkScalingJokers:
    """If we can already win, exploit spare hands/discards to scale jokers.

    Uses the scaling registry to pick optimal milk actions per trigger type
    and computes a milk budget (free hands available before winning).
    """
    name = "milk_scaling_jokers"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.play_policy import choose_milk_play
        ctx = RoundContext.from_state(state)
        return choose_milk_play(ctx)


class SellLuchador:
    """Sell Luchador to disable a boss blind effect — last resort when losing.

    Only fires when:
    - Luchador is owned
    - Current blind is a boss blind
    - At least one hand has been played (let milking happen first)
    - Projected score can't beat the blind (we're going to die)
    """
    name = "sell_luchador"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.playing import choose_sell_luchador
        ctx = RoundContext.from_state(state)
        return choose_sell_luchador(ctx)


class PlayWinningHand:
    """If the best hand beats the remaining blind score, play it."""
    name = "play_winning_hand"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.play_policy import choose_winning_play
        ctx = RoundContext.from_state(state)
        return choose_winning_play(ctx)


class PlayHighValueHand:
    """Play a high-scoring hand even if it won't win, using round projection.

    Factors in hand-saving economy: each unused hand at round end is worth $1.
    At lower antes ($1 compounds via interest), we raise the play threshold
    when comfortable to avoid wasting hands on marginal contributions.
    """
    name = "play_high_value_hand"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.play_policy import choose_high_value_play
        ctx = RoundContext.from_state(state)
        return choose_high_value_play(ctx)


class DiscardToImprove:
    """
    If we have discards left and the best hand can't win this turn,
    try to improve by discarding.

    Uses Monte Carlo sampling for expected value comparison:
      chase_ev = hit_prob * improved_score + (1 - hit_prob) * miss_ev
      play_ev  = current_best_score

    miss_ev is estimated by sampling random draws from the deck and
    evaluating best_hand() on each. Candidates sharing the same keep
    set share one sampling pass.

    Discard if chase_ev > play_ev.
    """
    name = "discard_to_improve"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.discard_policy import choose_discard
        ctx = RoundContext.from_state(state)
        return choose_discard(ctx)

    # Backward-compatible classmethods — delegate to discard_policy module functions
    @staticmethod
    def _chase_ev(candidate, ctx, miss_ev):
        from balatro_bot.domain.policy.discard_policy import _chase_ev
        return _chase_ev(candidate, ctx, miss_ev)

    @staticmethod
    def _sample_miss_ev(keep_indices, ctx):
        from balatro_bot.domain.policy.discard_policy import _sample_miss_ev
        return _sample_miss_ev(keep_indices, ctx)

    @classmethod
    def _best_chase(cls, suggestions, ctx, play_ev):
        from balatro_bot.domain.policy.discard_policy import _best_chase
        return _best_chase(suggestions, ctx, play_ev)


class PlayBestAvailable:
    """Last resort: play the best hand we have, even if it won't clear."""
    name = "play_best_available"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.play_policy import choose_best_available
        ctx = RoundContext.from_state(state)
        return choose_best_available(ctx)


