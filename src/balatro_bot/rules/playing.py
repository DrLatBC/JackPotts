from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from balatro_bot.actions import PlayCards, DiscardCards, SellJoker, Action
from balatro_bot.context import RoundContext
from balatro_bot.scaling import (
    SCALING_REGISTRY, PLAY_SCALERS, DISCARD_SCALERS,
    FINAL_HAND_JOKERS, SELL_PROTECTED, ANTI_DISCARD, DECAY_JOKERS,
)
from balatro_bot.constants import FACE_RANKS_SET
from balatro_bot.cards import card_rank, rank_value, is_debuffed
from balatro_bot.hand_evaluator import best_hand, classify_hand, cards_not_in, discard_candidates, score_hand, ChaseCandidate
from balatro_bot.rules._helpers import _pad_with_junk, _sort_play_order

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
        # The Wall: 4x blind needs much more margin before milking
        if ctx.blind_name == "The Wall":
            margin = 2.0
        else:
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
            indices = _sort_play_order([i for i, _ in junk[:4]], ctx.hand_cards, ctx.jokers)
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
                indices = _sort_play_order([required] + filler, ctx.hand_cards, ctx.jokers)
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
                indices = _sort_play_order([required] + filler, ctx.hand_cards, ctx.jokers)
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
                    indices = _sort_play_order(match.card_indices, ctx.hand_cards, ctx.jokers)
                    return PlayCards(
                        indices,
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
            indices = _sort_play_order([i for i, _ in dump], ctx.hand_cards, ctx.jokers)
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
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers, ctx.best.hand_name)
            indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}) >= {ctx.chips_remaining} needed",
                hand_name=ctx.best.hand_name,
            )
        return None

class PlayHighValueHand:
    """Play a high-scoring hand even if it won't win, using round projection.

    Factors in hand-saving economy: each unused hand at round end is worth $1.
    At lower antes ($1 compounds via interest), we raise the play threshold
    when comfortable to avoid wasting hands on marginal contributions.
    """
    name = "play_high_value_hand"

    # Minimum per-hand contribution thresholds by outlook.
    # "comfortable" — require meaningful contribution (don't waste $1 hands).
    # "tight" — need solid contributions each hand.
    # "hopeless" — defer to discard if possible, otherwise play anything.
    OUTLOOK_THRESHOLDS = {
        "won": 0.0,
        "comfortable": 0.10,
        "tight": 0.15,
        "hopeless": 0.0,
    }

    # At lower antes, each saved hand is worth more (compounds via interest).
    # Raise the comfortable threshold to avoid wasting hands on weak plays.
    # At higher antes, money matters less — scoring is everything.
    HAND_SAVE_ANTE_THRESHOLD = 5  # apply hand-saving bonus at ante <= this

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None

        # The Needle: only 1 hand play — never play unless it wins
        if ctx.blind_name == "The Needle":
            return None

        outlook = ctx.round_outlook
        effective_score = ctx.best.total * ctx.score_discount

        # Hopeless + discards available → defer to DiscardToImprove
        if outlook == "hopeless" and ctx.discards_left > 0:
            log.info("PlayHighValueHand: defer (hopeless, %d discards left)", ctx.discards_left)
            return None

        threshold = self.OUTLOOK_THRESHOLDS.get(outlook, 0.15)

        # Hand-saving economy: at lower antes when comfortable, raise the
        # bar for playing — each hand saved is $1 at cash-out, and $1 early
        # compounds via interest. Don't waste hands on 5-10% contributions
        # when we can win with fewer plays.
        if outlook == "comfortable" and ctx.ante <= self.HAND_SAVE_ANTE_THRESHOLD:
            old_threshold = threshold
            threshold = max(threshold, 0.20)
            if threshold > old_threshold:
                log.info("PlayHighValueHand: hand-save bump %.0f%%->%.0f%% (ante %d)", old_threshold * 100, threshold * 100, ctx.ante)

        # Last hand — play anything, no alternative
        if ctx.hands_left <= 1:
            threshold = 0.0

        if ctx.chips_remaining > 0 and effective_score < ctx.chips_remaining * threshold and ctx.discards_left > 0:
            log.info(
                "PlayHighValueHand: defer (%s for %d = %.0f%% of %d, need %.0f%%, outlook=%s)",
                ctx.best.hand_name, ctx.best.total,
                effective_score / ctx.chips_remaining * 100 if ctx.chips_remaining else 0,
                ctx.chips_remaining, threshold * 100, outlook,
            )
            return None

        indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers, ctx.best.hand_name)
        indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers)
        return PlayCards(
            indices,
            reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}), outlook={outlook}, {ctx.chips_remaining} remaining",
            hand_name=ctx.best.hand_name,
        )


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

    N_SAMPLES = 10  # Monte Carlo samples per unique keep set

    # Jokers that LOSE value when discards are used — be conservative with discards.
    # Only discard when the outlook is hopeless (discard bonus is irrelevant if we lose).
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

        outlook = ctx.round_outlook
        play_ev = ctx.best.total
        log.info(
            "DiscardToImprove: outlook=%s, best=%s for %d, chips_remaining=%d, hands=%d, discards=%d",
            outlook, ctx.best.hand_name, play_ev, ctx.chips_remaining, ctx.hands_left, ctx.discards_left,
        )

        # If best hand is already 5 cards (Flush, Straight, Full House, etc.),
        # discarding non-hand cards can't improve it. Only chase draws matter —
        # UNLESS we have extra cards in hand beyond the 5 AND the outlook is
        # hopeless, in which case discarding the extras cycles dead weight
        # into fresh draws.
        if len(ctx.best.card_indices) >= 5:
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
            best_chase = self._best_chase(suggestions, ctx, play_ev)
            if best_chase is not None:
                return best_chase

            # Desperation cycle: extra cards in hand + hopeless outlook
            extra_count = len(ctx.hand_cards) - 5
            if extra_count > 0 and outlook == "hopeless":
                extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices), rank_affinity=ctx.strategy.rank_affinity_dict(), scoring_suit=ctx.scoring_suit)
                to_discard = extras[:min(extra_count, ctx.discards_left)]
                if to_discard:
                    return DiscardCards(
                        to_discard,
                        reason=f"desperation cycle ({outlook}): {ctx.best.hand_name} for {ctx.best.total} vs {ctx.chips_remaining} needed",
                    )
            return None

        joker_keys = {j.get("key") for j in ctx.jokers}

        # If we have jokers that reward keeping discards, be conservative —
        # but only when the outlook isn't hopeless (if we're losing, the
        # discard bonus is irrelevant).
        has_keep_discard_jokers = bool(joker_keys & self.KEEP_DISCARDS_JOKERS)
        if has_keep_discard_jokers and outlook != "hopeless":
            return None

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

        # Try EV-based chase first
        best_chase = self._best_chase(suggestions, ctx, play_ev)
        if best_chase is not None:
            return best_chase

        # Discard dead cards when hopeless or tight AND hand uses < 5 cards
        if outlook in ("hopeless", "tight") and len(ctx.best.card_indices) < 5:
            for candidate in suggestions:
                if "chase" not in candidate.reason:
                    return DiscardCards(candidate.discard_indices, reason=candidate.reason)

        # Last resort: hopeless outlook, hand < 5 cards — discard something
        if suggestions and outlook == "hopeless" and len(ctx.best.card_indices) < 5:
            candidate = suggestions[0]
            return DiscardCards(candidate.discard_indices, reason=f"discard to improve ({outlook}): {candidate.reason}")

        return None

    @classmethod
    def _sample_miss_ev(cls, keep_indices: list[int], ctx: RoundContext) -> float:
        """Monte Carlo estimate of hand value after a failed chase.

        Draws N_SAMPLES random hands from the deck (keeping the specified
        cards), evaluates best_hand() on each, and returns the average score.
        """
        keep_cards = [ctx.hand_cards[i] for i in keep_indices]
        discard_count = len(ctx.hand_cards) - len(keep_cards)
        draw_pile = ctx.deck_cards

        if not draw_pile or len(draw_pile) < discard_count:
            return ctx.best.total if ctx.best else 0

        total = 0
        for _ in range(cls.N_SAMPLES):
            drawn = random.sample(draw_pile, discard_count)
            new_hand = keep_cards + drawn
            result = best_hand(
                new_hand,
                hand_levels=ctx.hand_levels,
                jokers=ctx.jokers,
                money=ctx.money,
                discards_left=max(0, ctx.discards_left - 1),
                hands_left=ctx.hands_left,
                ancient_suit=ctx.ancient_suit,
            )
            total += result.total if result else 0
        return total / cls.N_SAMPLES

    @staticmethod
    def _chase_ev(candidate: ChaseCandidate, ctx: RoundContext, miss_ev: float) -> float:
        """Expected value of taking a chase discard.

        miss_ev is pre-computed via _sample_miss_ev for the candidate's keep set.
        """
        if candidate.chase_hand == "redraw":
            # Redraw has no specific target — miss_ev IS the expected value
            return miss_ev

        keep_cards = [ctx.hand_cards[i] for i in candidate.keep_indices]
        # held_cards is empty: all kept cards are played, discarded cards are gone
        _, _, improved = score_hand(
            candidate.chase_hand,
            keep_cards,
            hand_levels=ctx.hand_levels,
            jokers=ctx.jokers,
            played_cards=keep_cards,
            held_cards=[],
            money=ctx.money,
            discards_left=max(0, ctx.discards_left - 1),
            hands_left=ctx.hands_left,
            ancient_suit=ctx.ancient_suit,
        )

        return candidate.hit_prob * improved + (1 - candidate.hit_prob) * miss_ev

    @classmethod
    def _best_chase(cls, suggestions: list[ChaseCandidate], ctx: RoundContext, play_ev: float) -> DiscardCards | None:
        """Find the best chase candidate whose EV exceeds play_ev.

        Groups candidates by keep set so Monte Carlo sampling is shared
        across candidates that discard the same cards.
        """
        # Build miss_ev cache — one sample pass per unique keep set
        miss_ev_cache: dict[tuple[int, ...], float] = {}
        for candidate in suggestions:
            if "chase" not in candidate.reason:
                continue
            key = tuple(sorted(candidate.keep_indices))
            if key not in miss_ev_cache:
                miss_ev_cache[key] = cls._sample_miss_ev(candidate.keep_indices, ctx)
                log.info("MC miss_ev for keep=%s: %.0f (play_ev=%.0f)", key, miss_ev_cache[key], play_ev)

        # Find best chase by EV
        best = None
        best_ev = play_ev  # only chase if EV strictly exceeds playing

        for candidate in suggestions:
            if "chase" not in candidate.reason:
                continue
            key = tuple(sorted(candidate.keep_indices))
            ev = cls._chase_ev(candidate, ctx, miss_ev_cache[key])
            log.info(
                "chase EV: %s %.0f%% -> EV %.0f (miss=%.0f, play=%.0f) %s",
                candidate.chase_hand, candidate.hit_prob * 100, ev,
                miss_ev_cache[key], play_ev,
                "ACCEPT" if ev > best_ev else "reject",
            )
            if ev > best_ev:
                best_ev = ev
                best = candidate

        if best is not None:
            margin = best_ev / play_ev if play_ev > 0 else float("inf")
            log.info("chase ACCEPTED: %s EV %.0f vs play %.0f (%.1fx)", best.chase_hand, best_ev, play_ev, margin)
            return DiscardCards(
                best.discard_indices,
                reason=f"{best.reason} [EV {best_ev:.0f} vs play {play_ev:.0f}, {margin:.1f}x]",
            )
        if miss_ev_cache:
            log.info("all chases rejected (play_ev=%.0f beats all)", play_ev)
        return None


class PlayBestAvailable:
    """Last resort: play the best hand we have, even if it won't clear."""
    name = "play_best_available"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)

        # The Needle: keep discarding if we have discards — don't give up
        if ctx.blind_name == "The Needle" and ctx.discards_left > 0:
            if ctx.best:
                keep = set(ctx.best.card_indices)
                to_discard = cards_not_in(ctx.hand_cards, keep, rank_affinity=ctx.strategy.rank_affinity_dict(), scoring_suit=ctx.scoring_suit)[:min(5, ctx.discards_left)]
                if to_discard:
                    return DiscardCards(to_discard, reason="Needle: use all discards to find winning hand")
            return None

        # If hand can't win and we still have discards AND the hand is < 5 cards,
        # discard junk to try to improve. 5-card hands can't be improved by discarding.
        if (ctx.best and ctx.best.total < ctx.chips_remaining
                and ctx.discards_left > 0 and len(ctx.best.card_indices) < 5):
            keep = set(ctx.best.card_indices)
            to_discard = cards_not_in(ctx.hand_cards, keep, rank_affinity=ctx.strategy.rank_affinity_dict(), scoring_suit=ctx.scoring_suit)[:min(5, ctx.discards_left)]
            if to_discard:
                return DiscardCards(to_discard, reason="last resort discard (hand too weak, searching for better)")
        if ctx.best:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers, ctx.best.hand_name)
            indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers)
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
                indices = _pad_with_junk(unconstrained.card_indices, ctx.hand_cards, ctx.jokers, unconstrained.hand_name)
                indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers)
                return PlayCards(
                    indices,
                    reason=f"mouth locked ({ctx.mouth_locked_hand}) but can't form it: "
                           f"playing {unconstrained.hand_name} for {unconstrained.total}",
                    hand_name=unconstrained.hand_name,
                )
        # Absolute fallback: play best 5-card combo we can find
        if ctx.hand_cards:
            if len(ctx.hand_cards) >= 5:
                ranked = sorted(range(len(ctx.hand_cards)),
                                key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"),
                                reverse=True)
                indices = _sort_play_order(ranked[:5], ctx.hand_cards, ctx.jokers)
                return PlayCards(indices, reason="fallback: play 5 highest cards", hand_name="High Card")
            indices = _sort_play_order(list(range(len(ctx.hand_cards))), ctx.hand_cards, ctx.jokers)
            return PlayCards(indices, reason="fallback: play all remaining cards", hand_name="High Card")
        return None
