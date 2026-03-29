from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import PlayCards, DiscardCards, SellJoker, Action
from balatro_bot.context import RoundContext
from balatro_bot.scaling import (
    SCALING_REGISTRY, PLAY_SCALERS, DISCARD_SCALERS,
    FINAL_HAND_JOKERS, SELL_PROTECTED, ANTI_DISCARD, DECAY_JOKERS,
)
from balatro_bot.constants import FACE_RANKS_SET
from balatro_bot.cards import card_rank, rank_value, is_debuffed
from balatro_bot.hand_evaluator import best_hand, classify_hand, cards_not_in, discard_candidates
from balatro_bot.rules._helpers import _pad_with_junk

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class VerdantLeafUnlock:
    """Sell weakest joker to lift Verdant Leaf's full-debuff on all cards.

    Verdant Leaf debuffs every playing card until one joker is sold.
    Fires immediately on the first SELECTING_HAND tick — sell the cheapest
    non-scaling joker, then normal play resumes.
    """
    name = "verdant_leaf_unlock"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if ctx.blind_name != "Verdant Leaf":
            return None
        # Check if debuff is still active (any hand card is debuffed)
        if not any(is_debuffed(c) for c in ctx.hand_cards):
            return None
        # Find the weakest joker to sacrifice (lowest sell value, non-scaling first)
        candidates = [
            (i, j) for i, j in enumerate(ctx.jokers)
            if j.get("key") not in SELL_PROTECTED
        ]
        if not candidates:
            # All jokers are scaling — sell the cheapest one anyway
            candidates = list(enumerate(ctx.jokers))
        if not candidates:
            return None
        sell_idx = min(candidates, key=lambda x: x[1].get("cost", {}).get("sell", 99))[0]
        label = ctx.jokers[sell_idx].get("label", "?")
        return SellJoker(sell_idx, reason=f"Verdant Leaf: sell {label} to unlock debuffed cards")


class MilkScalingJokers:
    """If we can already win, exploit spare hands/discards to scale jokers.

    Uses the scaling registry to pick optimal milk actions per trigger type
    and computes a milk budget (free hands available before winning).
    """
    name = "milk_scaling_jokers"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        effective = ctx.best.total * ctx.score_discount if ctx.best else 0
        if not ctx.best or effective < ctx.chips_remaining:
            return None

        # The Mouth: milking plays a different hand type, which locks us out
        # of our winning hand. Never milk against The Mouth.
        if ctx.mouth_locked_hand is not None or ctx.blind_name == "The Mouth":
            return None

        joker_keys = {j.get("key") for j in ctx.jokers}
        owned_scalers = {k: SCALING_REGISTRY[k] for k in joker_keys if k in SCALING_REGISTRY}
        active = {k: p for k, p in owned_scalers.items() if p.milk_priority > 0}
        if not active:
            return None

        # --- Milk budget: how many free hands do we have? ---
        # Acrobat/Dusk: winning hand should be the LAST hand. No safety buffer.
        has_final_hand = bool(joker_keys & FINAL_HAND_JOKERS)
        safety = 0 if has_final_hand else 1
        free_hands = ctx.hands_left - 1 - safety  # 1 for the winning hand
        if free_hands <= 0:
            return None

        # Comfort check: don't milk if barely winning (need some margin for variance)
        margin = 1.25 if free_hands >= 3 else 1.5
        if effective < ctx.chips_remaining * margin:
            return None

        # --- Phase 1: Discard milking (free, doesn't cost hands) ---
        # Resolve anti-discard conflicts: Banner (+30 chips/discard) vs
        # Mystic Summit (+15 mult at 0 discards) vs discard scalers (Castle/Yorick).
        has_banner = "j_banner" in joker_keys
        has_mystic = "j_mystic_summit" in joker_keys
        has_green = "j_green_joker" in joker_keys  # loses -1 mult per discard

        # Mystic Summit's +15 mult almost always beats Banner's +30 chips/discard.
        # Only preserve discards for Banner when Mystic Summit is NOT owned.
        should_preserve_discards = (has_banner and not has_mystic) or has_green

        if not should_preserve_discards and ctx.discards_left > 0:
            discard_action = self._milk_discard(ctx, joker_keys, active)
            if discard_action:
                return discard_action

        # --- Phase 2: Mystic Summit — burn all discards to activate +15 mult ---
        if has_mystic and ctx.discards_left > 0 and not has_green:
            keep = set(ctx.best.card_indices) if ctx.best else set()
            burnable = [
                (i, rank_value(card_rank(ctx.hand_cards[i]) or "2"))
                for i in range(len(ctx.hand_cards)) if i not in keep
            ]
            burnable.sort(key=lambda x: x[1])
            n = min(5, ctx.discards_left, len(burnable))
            if n > 0:
                indices = [i for i, _ in burnable[:n]]
                return DiscardCards(
                    indices,
                    reason=f"milk: burn {n} for Mystic Summit ({ctx.discards_left} discards left)",
                )

        # --- Phase 3: Play milking (costs a hand) ---
        play_scalers = {k: p for k, p in active.items()
                       if p.trigger.startswith("play")}
        final_hand_scalers = {k: p for k, p in active.items()
                             if p.trigger == "final_hand"}

        if play_scalers or final_hand_scalers:
            return self._milk_play(ctx, joker_keys, play_scalers, final_hand_scalers, free_hands)

        return None

    def _milk_discard(self, ctx: RoundContext, joker_keys: set,
                      active: dict[str, ScalingProfile]) -> Action | None:
        """Try to milk via discards (doesn't cost a hand play)."""

        # Hit the Road: discard Jacks (highest priority discard scaler)
        if "j_hit_the_road" in active:
            jacks = [i for i, c in enumerate(ctx.hand_cards) if card_rank(c) == "J"]
            if jacks:
                n = min(len(jacks), ctx.discards_left, 5)
                return DiscardCards(
                    jacks[:n],
                    reason=f"milk: discard {n} Jack(s) for Hit the Road",
                )

        # Castle / Yorick: discard low-value cards
        if any(k in active for k in ("j_castle", "j_yorick")):
            n = min(5, ctx.discards_left, len(ctx.hand_cards))
            if n > 0:
                # Discard weakest cards, but keep the winning hand's cards
                keep = set(ctx.best.card_indices) if ctx.best else set()
                discardable = [
                    (i, rank_value(card_rank(ctx.hand_cards[i]) or "2"))
                    for i in range(len(ctx.hand_cards)) if i not in keep
                ]
                discardable.sort(key=lambda x: x[1])
                indices = [i for i, _ in discardable[:n]]
                if indices:
                    names = [k for k in ("j_castle", "j_yorick") if k in active]
                    return DiscardCards(
                        indices,
                        reason=f"milk: discard {len(indices)} cards for {', '.join(names)}",
                    )

        return None

    def _milk_play(self, ctx: RoundContext, joker_keys: set,
                   play_scalers: dict, final_hand_scalers: dict,
                   free_hands: int) -> Action | None:
        """Pick the optimal milk hand to play.

        Three categories of milk play:
        1. Card-property triggers: need a specific card (enhanced, rank=2, etc.)
        2. Hand-type triggers: need a specific poker hand (Straight, Two Pair, etc.)
        3. Generic triggers: any hand works — dump max junk for deck cycling

        All milk plays protect the winning hand's cards and play as many
        non-winning junk cards as possible for deck cycling.
        """
        from balatro_bot.cards import _modifier
        from balatro_bot.hand_evaluator import (
            enumerate_hands, discard_candidates as _dc,
        )

        avoid_faces = "j_ride_the_bus" in joker_keys
        hands_after = ctx.hands_left - 1
        keep = set(ctx.best.card_indices) if ctx.best else set()

        # Build playable card list (weakest first, optionally avoiding faces)
        playable = []
        for i, c in enumerate(ctx.hand_cards):
            r = card_rank(c)
            if not r:
                continue
            if avoid_faces and r in FACE_RANKS_SET:
                continue
            playable.append((i, rank_value(r)))
        playable.sort(key=lambda x: x[1])

        if not playable:
            return None

        # Junk = playable cards NOT in the winning hand
        junk = [(i, rv) for i, rv in playable if i not in keep]

        # --- Category 1: Card-property triggers ---

        # Square Joker: exactly 4 junk cards
        if "j_square" in play_scalers and len(junk) >= 4:
            indices = [i for i, _ in junk[:4]]
            return PlayCards(
                indices,
                reason=f"milk: cycle 4 junk for Square (+4 chips) ({hands_after} hands left)",
                hand_name=classify_hand([ctx.hand_cards[i] for i in indices]),
            )

        # Vampire: must include an enhanced card, pad with junk for cycling
        if "j_vampire" in play_scalers:
            enhanced = [
                (i, rv) for i, rv in junk
                if _modifier(ctx.hand_cards[i]).get("enhancement")
            ]
            if enhanced:
                required = enhanced[0][0]
                filler = [i for i, _ in junk if i != required][:4]
                indices = [required] + filler
                hand_name = classify_hand([ctx.hand_cards[i] for i in indices])
                return PlayCards(
                    indices,
                    reason=f"milk: enhanced card + {len(filler)} junk for Vampire (+X0.1) ({hands_after} hands left)",
                    hand_name=hand_name,
                )

        # Wee Joker: must include a 2, pad with junk for cycling
        if "j_wee" in play_scalers:
            twos = [(i, v) for i, v in junk if v == 2]
            if twos:
                required = twos[0][0]
                filler = [i for i, _ in junk if i != required][:4]
                indices = [required] + filler
                hand_name = classify_hand([ctx.hand_cards[i] for i in indices])
                return PlayCards(
                    indices,
                    reason=f"milk: 2 + {len(filler)} junk for Wee (+8 chips) ({hands_after} hands left)",
                    hand_name=hand_name,
                )

        # --- Category 2: Hand-type triggers ---
        # Collect target hand types, try to form from current cards, chase if needed.

        targets: list[tuple[str, str]] = []  # (hand_type, joker_name)

        if "j_runner" in play_scalers:
            targets.extend([("Straight", "j_runner"), ("Straight Flush", "j_runner")])
        if "j_trousers" in play_scalers:
            targets.append(("Two Pair", "j_trousers"))
        if "j_supernova" in play_scalers:
            preferred = ctx.strategy.top_hand()
            if preferred:
                targets.append((preferred, "j_supernova"))
        if "j_hiker" in play_scalers:
            # Hiker buffs every SCORED card — want hands where all cards score
            for ht in ("Flush", "Straight", "Full House", "Straight Flush",
                        "Two Pair", "Three of a Kind", "Four of a Kind"):
                targets.append((ht, "j_hiker"))

        if targets:
            # Try to form any target from current hand
            candidates = enumerate_hands(
                ctx.hand_cards, ctx.hand_levels,
                jokers=ctx.jokers, joker_limit=len(ctx.jokers),
            )
            for ht, jname in targets:
                match = next((c for c in candidates if c.hand_name == ht), None)
                if match:
                    return PlayCards(
                        match.card_indices,
                        reason=f"milk: {ht} for {jname} ({hands_after} hands left)",
                        hand_name=ht,
                    )

            # Can't form any target — chase with a discard if affordable
            if ctx.discards_left > 0 and free_hands >= 2:
                for ht, jname in targets:
                    chases = _dc(
                        ctx.hand_cards, ctx.hand_levels,
                        jokers=ctx.jokers, required_hand=ht,
                        deck_cards=ctx.deck_cards,
                    )
                    if chases:
                        indices, reason = chases[0]
                        return DiscardCards(
                            indices,
                            reason=f"milk: chase {ht} for {jname} ({reason})",
                        )

        # --- Category 3: Generic triggers — dump max junk for cycling ---
        generic = {k for k, p in play_scalers.items()
                   if p.trigger in ("play", "play_no_face")}
        generic |= set(final_hand_scalers)
        # Supernova/Hiker fall through here if they couldn't form their target type
        if "j_supernova" in play_scalers:
            generic.add("j_supernova")
        if "j_hiker" in play_scalers:
            generic.add("j_hiker")

        if generic:
            dump = junk[:5] if junk else playable[:1]
            indices = [i for i, _ in dump]
            hand_name = classify_hand([ctx.hand_cards[i] for i in indices])
            return PlayCards(
                indices,
                reason=f"milk: cycle {len(indices)} junk for {', '.join(sorted(generic))} ({hands_after} hands left)",
                hand_name=hand_name,
            )

        return None


class SellLuchador:
    """Sell Luchador to disable a boss blind effect — last resort when losing.

    Only fires when:
    - Luchador is owned
    - Current blind is a boss blind
    - At least one hand has been played (let milking happen first)
    - Projected score can't beat the blind (we're going to die)
    """
    name = "sell_luchador"

    # Boss blind names — Luchador only matters against these
    BOSS_BLINDS = {
        "The Needle", "The Eye", "The Mouth", "The Psychic",
        "Crimson Heart", "The Flint", "The Plant", "The Head",
        "The Water", "The Window", "The Hook", "The Wall",
        "The Wheel", "The Arm", "The Club", "The Fish",
        "The Tooth", "The Mark", "The Ox", "The House",
        "The Pillar", "The Serpent", "The Goad", "Amber Acorn",
        "Verdant Leaf", "Violet Vessel", "Cerulean Bell",
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)

        # Only act against boss blinds
        if ctx.blind_name not in self.BOSS_BLINDS:
            return None

        # Find Luchador
        luchador_idx = next(
            (i for i, j in enumerate(ctx.jokers) if j.get("key") == "j_luchador"), None
        )
        if luchador_idx is None:
            return None

        # Don't sell on the very first hand — let milking happen first
        if ctx.chips_scored == 0 and ctx.hands_left > 1:
            return None

        # Project whether we can win: best hand * remaining hands vs chips needed
        best_score = ctx.best.total * ctx.score_discount if ctx.best else 0
        projected = best_score * ctx.hands_left
        if projected >= ctx.chips_remaining:
            return None  # we can still win, don't sell

        # We're going to die — sell Luchador to disable the boss
        return SellJoker(
            luchador_idx,
            reason=f"Luchador: sell to disable {ctx.blind_name} "
                   f"(projected {projected:.0f} < {ctx.chips_remaining} needed)",
        )


class PlayWinningHand:
    """If the best hand beats the remaining blind score, play it."""
    name = "play_winning_hand"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None
        effective_score = ctx.best.total * ctx.score_discount
        if effective_score >= ctx.chips_remaining:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}) >= {ctx.chips_remaining} needed",
                hand_name=ctx.best.hand_name,
            )
        return None

class PlayHighValueHand:
    """Play a high-scoring hand even if it won't win, based on hands remaining."""
    name = "play_high_value_hand"

    THRESHOLDS = {
        6: 0.15,
        5: 0.20,
        4: 0.25,
        3: 0.25,
        2: 0.20,
        1: 0.0,   # last hand — play anything, no alternative
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None

        threshold = self.THRESHOLDS.get(ctx.hands_left, 0.15 if ctx.hands_left > 6 else 0.0)
        effective_score = ctx.best.total * ctx.score_discount

        # If the hand meets the threshold, play it — even if it can't solo-clear.
        # A Full House for 60% of the blind is a good play with hands remaining.
        # Only defer to DiscardToImprove when the hand is BELOW the threshold
        # and we have discards to try improving.
        if effective_score < ctx.chips_remaining * threshold and ctx.discards_left > 0:
            return None

        if effective_score >= ctx.chips_remaining * threshold:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}) >= {threshold:.0%} of {ctx.chips_remaining} remaining",
                hand_name=ctx.best.hand_name,
            )
        return None


class DiscardToImprove:
    """
    If we have discards left and the best hand can't win this turn,
    try to improve by discarding.

    Two modes:
    1. Chase a draw (flush/straight) — always worth it with 2+ discards.
    2. Discard dead cards around current hand — only when the hand is
       hopeless (scores < 30% of what's needed). No point trimming fat
       around a Full House that covers 80% of the blind.
    """
    name = "discard_to_improve"

    # Below this fraction of chips_remaining, the hand is hopeless enough
    # to justify discarding dead cards even without a specific draw.
    # Scales with hands_left: more hands remaining = more tolerant of weak hands.
    HOPELESS_THRESHOLDS = {
        5: 0.15,
        4: 0.20,
        3: 0.25,
        2: 0.35,
    }
    HOPELESS_DEFAULT = 0.50  # 1 hand left: discard if < 50% (you're losing anyway)

    # If best hand covers less than this fraction of chips_remaining,
    # the hand is desperate enough to use the last discard.
    DESPERATE_THRESHOLD = 0.15

    # Jokers that LOSE value when discards are used — be conservative with discards.
    # Only discard when the hand is truly hopeless (< 20% coverage).
    KEEP_DISCARDS_JOKERS = {
        "j_banner",         # +30 chips per discard remaining — direct score loss
        "j_delayed_grat",   # $2 per unused discard — economy loss
        "j_green_joker",    # -1 mult per discard — permanent scaling loss
        "j_ramen",          # -0.01 xmult per card discarded — xmult decay
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if ctx.discards_left <= 0:
            return None
        if not ctx.best:
            return None
        # Only discard if the current best hand can't clear the blind
        if ctx.best.total >= ctx.chips_remaining:
            return None

        # If best hand is already 5 cards (Flush, Straight, Full House, etc.),
        # discarding non-hand cards can't improve it. Only chase draws matter —
        # UNLESS we have extra cards in hand beyond the 5 AND the hand is
        # catastrophically hopeless, in which case discarding the extras cycles
        # dead weight into fresh draws.
        if len(ctx.best.card_indices) >= 5:
            # Still allow chase draws — a better 5-card hand might exist
            strat_affinity = {ht: score for ht, score in ctx.strategy.preferred_hands}
            suggestions = discard_candidates(
                ctx.hand_cards, ctx.hand_levels,
                max_discard=min(5, ctx.discards_left),
                strategy_affinity=strat_affinity,
                deck_cards=ctx.deck_cards,
                chips_remaining=ctx.chips_remaining,
                jokers=ctx.jokers,
                required_hand=ctx.mouth_locked_hand,
            )
            for indices, reason in suggestions:
                if "chase" in reason:
                    return DiscardCards(indices, reason=reason)

            # Desperation: extra cards in hand + hand covers < 15% of needed.
            # Discard the non-scoring extras to cycle into fresh draws.
            extra_count = len(ctx.hand_cards) - 5
            if extra_count > 0 and ctx.chips_remaining > 0:
                coverage = ctx.best.total / ctx.chips_remaining
                if coverage < 0.15:
                    extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices), rank_affinity=ctx.strategy.rank_affinity_dict())
                    to_discard = extras[:min(extra_count, ctx.discards_left)]
                    if to_discard:
                        return DiscardCards(
                            to_discard,
                            reason=f"desperation cycle: {ctx.best.hand_name} for {ctx.best.total} is only {coverage:.0%} of {ctx.chips_remaining} needed",
                        )
            return None

        joker_keys = {j.get("key") for j in ctx.jokers}

        # If we have jokers that reward keeping discards, be conservative —
        # BUT only when the hand can actually contribute meaningfully.
        # If best hand can't even cover 20% of what's needed, the discard
        # bonus is irrelevant — we need a better hand or we lose.
        has_keep_discard_jokers = bool(joker_keys & self.KEEP_DISCARDS_JOKERS)
        if has_keep_discard_jokers:
            hand_covers = ctx.best.total / ctx.chips_remaining if ctx.chips_remaining > 0 else 1.0
            if hand_covers >= 0.20:
                # Hand is decent — save discards for the bonus
                return None

        # Build strategy affinity dict for discard weighting
        strat_affinity = {ht: score for ht, score in ctx.strategy.preferred_hands}

        suggestions = discard_candidates(
            ctx.hand_cards, ctx.hand_levels,
            max_discard=min(5, ctx.discards_left),
            strategy_affinity=strat_affinity,
            deck_cards=ctx.deck_cards,
            chips_remaining=ctx.chips_remaining,
            jokers=ctx.jokers,
            required_hand=ctx.mouth_locked_hand,
        )

        threshold = self.HOPELESS_THRESHOLDS.get(ctx.hands_left, self.HOPELESS_DEFAULT)
        hopeless = ctx.best.total < ctx.chips_remaining * threshold

        # If the hand already covers PlayHighValueHand's play threshold, don't
        # burn discards on weak chases — we can already play this hand. Only
        # accept chases with ≥50% hit probability; below that, just play.
        play_threshold = PlayHighValueHand.THRESHOLDS.get(
            ctx.hands_left, 0.15 if ctx.hands_left > 6 else 0.0
        )
        hand_is_playable = (
            ctx.chips_remaining > 0
            and ctx.best.total >= ctx.chips_remaining * play_threshold
        )

        for indices, reason in suggestions:
            if "chase" in reason:
                if hand_is_playable:
                    # Parse hit probability from reason: "chase X (NN% to hit)"
                    try:
                        chase_prob = int(reason.split("(")[1].split("%")[0]) / 100
                    except (IndexError, ValueError):
                        chase_prob = 1.0
                    if chase_prob < 0.50:
                        continue  # hand is playable — skip weak chase
                return DiscardCards(indices, reason=reason)
            # Discard dead cards when the hand is hopeless AND the
            # best hand uses < 5 cards (otherwise there are no dead cards
            # in the played hand — discarding around a Flush is pointless).
            if hopeless and len(ctx.best.card_indices) < 5:
                return DiscardCards(indices, reason=reason)

        # Last resort: if the hand can't win and we have discards, discard
        # SOMETHING — but only if the best hand uses < 5 cards. A 5-card hand
        # (Flush, Straight, Full House) can't be improved by discarding around it.
        if suggestions and len(ctx.best.card_indices) < 5:
            indices, reason = suggestions[0]
            return DiscardCards(indices, reason=f"discard to improve (below play threshold): {reason}")

        return None


class PlayBestAvailable:
    """Last resort: play the best hand we have, even if it won't clear."""
    name = "play_best_available"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        # If hand can't win and we still have discards AND the hand is < 5 cards,
        # discard junk to try to improve. 5-card hands can't be improved by discarding.
        if (ctx.best and ctx.best.total < ctx.chips_remaining
                and ctx.discards_left > 0 and len(ctx.best.card_indices) < 5):
            keep = set(ctx.best.card_indices)
            to_discard = cards_not_in(ctx.hand_cards, keep, rank_affinity=ctx.strategy.rank_affinity_dict())[:min(5, ctx.discards_left)]
            if to_discard:
                return DiscardCards(to_discard, reason="last resort discard (hand too weak, searching for better)")
        if ctx.best:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"best available: {ctx.best.hand_name} for {ctx.best.total}",
                hand_name=ctx.best.hand_name,
            )
        # No valid hand found (e.g. The Mouth locked to a hand type we can't form).
        # Try again without the constraint — playing any 5 cards is better than 1.
        if ctx.mouth_locked_hand and ctx.hand_cards:
            unconstrained = best_hand(
                ctx.hand_cards, ctx.hand_levels,
                min_select=ctx.min_cards, jokers=ctx.jokers,
                money=ctx.money, discards_left=ctx.discards_left,
                hands_left=ctx.hands_left,
            )
            if unconstrained:
                indices = _pad_with_junk(unconstrained.card_indices, ctx.hand_cards, ctx.jokers)
                return PlayCards(
                    indices,
                    reason=f"mouth locked ({ctx.mouth_locked_hand}) but can't form it: "
                           f"playing {unconstrained.hand_name} for {unconstrained.total}",
                    hand_name=unconstrained.hand_name,
                )
        # Absolute fallback: play best 5-card combo we can find
        if ctx.hand_cards:
            if len(ctx.hand_cards) >= 5:
                # Play the 5 highest-value cards
                ranked = sorted(range(len(ctx.hand_cards)),
                                key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"),
                                reverse=True)
                return PlayCards(ranked[:5], reason="fallback: play 5 highest cards", hand_name="High Card")
            return PlayCards(list(range(len(ctx.hand_cards))), reason="fallback: play all remaining cards", hand_name="High Card")
        return None
