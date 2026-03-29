"""
Rule-based decision engine for Balatro.

The engine is a priority list: rules are evaluated top-to-bottom and the first
rule that fires produces the action.  Each rule is a simple function that
receives the full game state and returns an Action or None (pass to next rule).

Architecture:
    GameState (dict) -> RuleEngine.decide() -> Action

Actions are thin wrappers that serialize to balatrobot JSON-RPC calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

log = logging.getLogger("balatro_bot")

from hand_evaluator import (
    HandCandidate,
    HAND_INFO,
    best_hand,
    cards_not_in,
    discard_candidates,
    enumerate_hands,
)
from strategy import Strategy, compute_strategy

if TYPE_CHECKING:
    from typing import Any


# ---------------------------------------------------------------------------
# Actions — what the bot can tell the game to do
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlayCards:
    """Play the given card indices."""
    card_indices: list[int]
    reason: str = ""
    hand_name: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "play", {"cards": self.card_indices}


@dataclass(frozen=True)
class DiscardCards:
    """Discard the given card indices."""
    card_indices: list[int]
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "discard", {"cards": self.card_indices}


@dataclass(frozen=True)
class SelectBlind:
    """Select (accept) the current blind."""
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "select", {}


@dataclass(frozen=True)
class SkipBlind:
    """Skip the current blind."""
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "skip", {}


@dataclass(frozen=True)
class CashOut:
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "cash_out", {}


@dataclass(frozen=True)
class NextRound:
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "next_round", {}


@dataclass(frozen=True)
class BuyCard:
    index: int
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "buy", {"card": self.index}


@dataclass(frozen=True)
class BuyPack:
    index: int
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "buy", {"pack": self.index}


@dataclass(frozen=True)
class BuyVoucher:
    index: int
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "buy", {"voucher": self.index}


@dataclass(frozen=True)
class SellJoker:
    index: int
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "sell", {"joker": self.index}


@dataclass(frozen=True)
class SellConsumable:
    index: int
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "sell", {"consumable": self.index}


@dataclass(frozen=True)
class Reroll:
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "reroll", {}


@dataclass(frozen=True)
class RearrangeJokers:
    order: list[int]  # 0-based indices in desired final order
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "rearrange", {"jokers": self.order}


@dataclass(frozen=True)
class UseConsumable:
    index: int
    target_cards: list[int] | None = None
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        params: dict[str, Any] = {"consumable": self.index}
        if self.target_cards is not None:
            params["cards"] = self.target_cards
        return "use", params


@dataclass(frozen=True)
class PackAction:
    """Pick a card from an opened pack, or skip."""
    card_index: int | None = None
    targets: list[int] | None = None
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        if self.card_index is None:
            return "pack", {"skip": True}
        params: dict[str, Any] = {"card": self.card_index}
        if self.targets is not None:
            params["targets"] = self.targets
        return "pack", params


Action = (
    PlayCards | DiscardCards | SelectBlind | SkipBlind | CashOut | NextRound
    | BuyCard | BuyPack | BuyVoucher | SellJoker | SellConsumable | Reroll
    | RearrangeJokers | UseConsumable | PackAction
)


# ---------------------------------------------------------------------------
# Rule protocol
# ---------------------------------------------------------------------------

class Rule(Protocol):
    """A rule returns an Action if it fires, or None to pass."""
    name: str
    def evaluate(self, state: dict[str, Any]) -> Action | None: ...


# ---------------------------------------------------------------------------
# Helper: extract common state info
# ---------------------------------------------------------------------------

@dataclass
class RoundContext:
    """Pre-computed facts about the current round for rules to use."""
    blind_score: int
    blind_name: str
    chips_scored: int
    chips_remaining: int
    hands_left: int
    discards_left: int
    hand_cards: list[dict]
    hand_levels: dict[str, dict]
    jokers: list[dict]
    best: HandCandidate | None
    money: int
    ante: int
    round_num: int
    min_cards: int
    strategy: Strategy
    deck_cards: list[dict]
    mouth_locked_hand: str | None = None  # The Mouth: hand type locked after first play
    score_discount: float = 1.0          # Crimson Heart: expected fraction of score (random joker disabled)
    forced_card_idx: int | None = None   # Cerulean Bell: index of the card forced into every play
    ancient_suit: str | None = None      # Ancient Joker's current rotating suit (H/D/C/S)

    @staticmethod
    def from_state(state: dict[str, Any]) -> RoundContext:
        cached = state.get("_round_ctx")
        if cached is not None:
            return cached

        hand_cards = state.get("hand", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        rnd = state.get("round", {})
        jokers = state.get("jokers", {}).get("cards", [])
        money = state.get("money", 0)
        deck_cards = state.get("cards", {}).get("cards", [])

        # Find the current blind's required score and name
        blind_score = 0
        blind_name = ""
        for b in state.get("blinds", {}).values():
            if isinstance(b, dict) and b.get("status") == "CURRENT":
                blind_score = b.get("score", 0)
                blind_name = b.get("name", "")
                break

        # Boss blind constraints
        min_cards = 5 if blind_name == "The Psychic" else 1

        # The Mouth: locks you to one hand type after your first play
        mouth_locked_hand = None
        if blind_name == "The Mouth":
            for hand_name, hand_data in hand_levels.items():
                if isinstance(hand_data, dict) and hand_data.get("played_this_round", 0) > 0:
                    mouth_locked_hand = hand_name
                    break

        chips_scored = rnd.get("chips", 0)
        hands_left = rnd.get("hands_left", 0)
        discards_left = rnd.get("discards_left", 0)

        # Crimson Heart: one random joker is disabled each hand
        # Expected score = (n-1)/n of estimate; use 0.5 floor if somehow 0 jokers
        joker_count = len(jokers)
        score_discount = (
            (joker_count - 1) / joker_count if blind_name == "Crimson Heart" and joker_count > 1
            else 0.5 if blind_name == "Crimson Heart"
            else 1.0
        )

        # Cerulean Bell: one card is always highlighted (forced into every play)
        # API sets state.highlight = true for the forced card
        forced_card_idx = None
        if blind_name == "Cerulean Bell":
            for i, c in enumerate(hand_cards):
                s = c.get("state", {})
                if isinstance(s, dict) and s.get("highlight"):
                    forced_card_idx = i
                    break

        ancient_suit = rnd.get("ancient_suit")

        strat = compute_strategy(jokers, hand_levels)

        ctx = RoundContext(
            blind_score=blind_score,
            blind_name=blind_name,
            chips_scored=chips_scored,
            chips_remaining=blind_score - chips_scored,
            hands_left=hands_left,
            discards_left=discards_left,
            hand_cards=hand_cards,
            hand_levels=hand_levels,
            jokers=jokers,
            best=best_hand(
                hand_cards, hand_levels,
                min_select=min_cards, jokers=jokers,
                money=money, discards_left=discards_left,
                hands_left=hands_left,
                joker_limit=state.get("jokers", {}).get("limit", 5),
                required_hand=mouth_locked_hand,
                required_card_indices={forced_card_idx} if forced_card_idx is not None else None,
                ancient_suit=ancient_suit,
            ),
            mouth_locked_hand=mouth_locked_hand,
            score_discount=score_discount,
            forced_card_idx=forced_card_idx,
            ancient_suit=ancient_suit,
            money=money,
            ante=state.get("ante_num", 1),
            round_num=state.get("round_num", 1),
            min_cards=min_cards,
            strategy=strat,
            deck_cards=deck_cards,
        )
        state["_round_ctx"] = ctx
        return ctx


# ---------------------------------------------------------------------------
# Rules: SELECTING_HAND
# ---------------------------------------------------------------------------

# Jokers that care about played card count — prevent padding
FEWER_CARDS_JOKERS = {"j_half"}       # +20 mult on ≤3 cards
ALL_SCORE_JOKERS = {"j_splash"}       # all played cards score
EXACT_4_JOKERS = {"j_square"}         # +4 chips on exactly 4 cards


def _pad_with_junk(
    card_indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
    max_cards: int = 5,
) -> list[int]:
    """Pad a hand with low-value junk cards for free deck cycling.

    Playing extra non-scoring cards doesn't change the hand score but
    removes them from the hand, drawing fresh replacements next hand.
    """
    from hand_evaluator import card_rank, rank_value

    joker_keys = {j.get("key") for j in jokers}
    n = len(card_indices)

    # Don't pad if jokers benefit from fewer cards
    if joker_keys & FEWER_CARDS_JOKERS and n <= 3:
        return card_indices
    if joker_keys & ALL_SCORE_JOKERS:
        return card_indices

    # Square Joker: never pad beyond 4
    if joker_keys & EXACT_4_JOKERS:
        target = 4
    else:
        target = max_cards

    if n >= target:
        return card_indices

    # Find junk cards not already in the hand.
    # Debuffed cards sort first (they contribute nothing — best to cycle out).
    # Then sort by rank value ascending (weakest first).
    from hand_evaluator import is_debuffed
    used = set(card_indices)
    junk = []
    for i, c in enumerate(hand_cards):
        if i not in used:
            r = card_rank(c)
            debuffed = is_debuffed(c)
            # Sort key: debuffed first (0), then by rank ascending
            junk.append((i, 0 if debuffed else 1, rank_value(r) if r else 0))
    junk.sort(key=lambda x: (x[1], x[2]))

    # Pad with the weakest junk (debuffed first)
    padded = list(card_indices)
    for i, _, _ in junk:
        if len(padded) >= target:
            break
        padded.append(i)

    return padded


# Joker keys that benefit from extra plays/discards before winning
PLAY_SCALERS = {"j_green_joker", "j_supernova", "j_ride_the_bus", "j_trousers", "j_runner", "j_square"}
FINAL_HAND_JOKERS = {"j_acrobat", "j_dusk"}
DISCARD_SCALERS = {"j_castle", "j_hit_the_road"}
# Face ranks that reset Ride the Bus
FACE_RANKS_SET = {"J", "Q", "K"}


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
        from hand_evaluator import is_debuffed
        if not any(is_debuffed(c) for c in ctx.hand_cards):
            return None
        # Find the weakest joker to sacrifice (lowest sell value, non-scaling first)
        candidates = [
            (i, j) for i, j in enumerate(ctx.jokers)
            if j.get("key") not in SCALING_JOKERS
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
    """If we can already win, play weak hands first to scale jokers."""
    name = "milk_scaling_jokers"

    # Only milk if winning hand covers this much of the blind
    COMFORT_MARGIN = 1.5
    # Keep at least this many hands as safety buffer
    MIN_HANDS_RESERVE = 2

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None

        # Can we already win?
        if ctx.best.total < ctx.chips_remaining:
            return None

        joker_keys = {j.get("key") for j in ctx.jokers}
        has_play_scalers = bool(joker_keys & PLAY_SCALERS)
        has_final_hand = bool(joker_keys & FINAL_HAND_JOKERS)
        has_discard_scalers = bool(joker_keys & DISCARD_SCALERS)

        if not (has_play_scalers or has_final_hand or has_discard_scalers):
            return None

        # Need comfortable margin — don't milk if barely winning
        if ctx.best.total < ctx.chips_remaining * self.COMFORT_MARGIN:
            return None

        # Need spare hands to milk with
        if ctx.hands_left <= self.MIN_HANDS_RESERVE:
            return None

        # Milk via discard first (doesn't cost a hand play)
        if has_discard_scalers and ctx.discards_left > 0:
            from hand_evaluator import card_rank
            # Hit the Road: discard Jacks
            if "j_hit_the_road" in joker_keys:
                for i, c in enumerate(ctx.hand_cards):
                    if card_rank(c) == "J":
                        return DiscardCards(
                            [i], reason=f"milk: discard Jack for Hit the Road scaling",
                        )
            # Castle: discard any card (all contribute)
            if "j_castle" in joker_keys and ctx.hand_cards:
                # Discard lowest-value card
                from hand_evaluator import rank_value
                worst = min(range(len(ctx.hand_cards)),
                           key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"))
                return DiscardCards(
                    [worst], reason=f"milk: discard for Castle scaling",
                )

        # Milk via weak hand play
        if has_play_scalers or has_final_hand:
            from hand_evaluator import card_rank, rank_value

            # Avoid face cards if Ride the Bus is in play
            avoid_faces = "j_ride_the_bus" in joker_keys

            # Sort hand cards by rank (weakest first), filtering faces if needed
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

            scalers = (joker_keys & PLAY_SCALERS) | (joker_keys & FINAL_HAND_JOKERS)

            # Square Joker: play exactly 4 cards to trigger +4 chips scaling
            if "j_square" in joker_keys and len(playable) >= 4:
                milk_indices = [i for i, _ in playable[:4]]
                return PlayCards(
                    milk_indices,
                    reason=f"milk: play 4 cards for Square Joker (+4 chips) ({ctx.hands_left - 1} hands left after)",
                    hand_name="High Card",
                )

            # Default: play single weakest card as High Card
            best_milk_idx = playable[0][0]
            return PlayCards(
                [best_milk_idx],
                reason=f"milk: play weak card to scale {', '.join(sorted(scalers))} ({ctx.hands_left - 1} hands left after)",
                hand_name="High Card",
            )

        return None


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

    # Jokers that benefit from keeping discards — don't waste discards when these are active
    KEEP_DISCARDS_JOKERS = {
        "j_mystic_summit",  # +15 mult at 0 discards remaining
        "j_banner",         # +30 chips per discard remaining
        "j_delayed_grat",   # $2 per unused discard at end of round
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
                    extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices))
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
            from hand_evaluator import card_rank, rank_value, is_debuffed, cards_not_in
            keep = set(ctx.best.card_indices)
            to_discard = cards_not_in(ctx.hand_cards, keep)[:min(5, ctx.discards_left)]
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
                from hand_evaluator import card_rank, rank_value
                ranked = sorted(range(len(ctx.hand_cards)),
                                key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"),
                                reverse=True)
                return PlayCards(ranked[:5], reason="fallback: play 5 highest cards", hand_name="High Card")
            return PlayCards(list(range(len(ctx.hand_cards))), reason="fallback: play all remaining cards", hand_name="High Card")
        return None


# ---------------------------------------------------------------------------
# Rules: BLIND_SELECT
# ---------------------------------------------------------------------------

class AlwaysSelectBlind:
    """Simple baseline: always accept the blind."""
    name = "always_select_blind"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return SelectBlind(reason="baseline: always select")


class SkipForTag:
    """
    Skip small/big blind if the tag reward is valuable enough.
    Placeholder — fill in tag evaluation logic.
    """
    name = "skip_for_tag"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        # TODO: Evaluate tag rewards and skip when beneficial
        # For now, never skip
        return None


# ---------------------------------------------------------------------------
# Rules: SHOP
# ---------------------------------------------------------------------------

# Jokers that accumulate value over the run (or round). Protect from selling
# and don't buy Madness when any of these are owned (Madness eats a random joker).
SCALING_JOKERS = {
    # Permanent xmult scalers
    "j_campfire",        # X0.25 per sell
    "j_constellation",   # X0.1 per planet used
    "j_vampire",         # X0.1 per enhanced card played
    "j_hologram",        # X0.25 per card added to deck
    "j_lucky_cat",       # X0.25 per Lucky trigger
    "j_canio",           # X1 per face card destroyed
    # Per-round xmult
    "j_hit_the_road",    # X0.5 per Jack discarded this round
    # Scaling mult
    "j_red_card",        # +3 mult per pack skip
    "j_green_joker",     # +1 mult per hand played
}

class SellWeakJoker:
    """Sell the weakest joker if slots are full and the shop has a better one."""
    name = "sell_weak_joker"

    # Approximate power tier for unconditional jokers (no hand type affinity).
    # Higher = harder to justify selling.
    UNCONDITIONAL_POWER: dict[str, float] = {
        # xmult — never sell these
        "j_cavendish": 5.0,    # X3
        "j_madness": 6.0,     # scaling X0.5 per blind — run-defining
        "j_stencil": 4.0,     # X1 per empty slot
        # Strong +mult
        "j_gros_michel": 3.0, # +15 mult
        "j_popcorn": 2.5,     # +20 mult (decays)
        "j_misprint": 1.5,    # +11 avg mult
        # Scaling — get better over time, don't sell late
        "j_supernova": 2.5,   # +mult per time hand type played this run
        "j_green_joker": 2.0, # +1 mult per hand, scaling
        "j_ride_the_bus": 2.0,# +1 mult per hand without face card
        "j_flash": 2.0,       # +2 mult per reroll
        "j_constellation": 3.0,# X0.1 per planet used — scaling xmult
        "j_campfire": 2.5,    # X0.25 per sell
        # Moderate
        "j_ice_cream": 1.5,   # +100 chips (decays)
        "j_blue_joker": 1.2,  # +2 chips/deck card
        "j_stuntman": 2.0,    # +250 chips
        # Weak
        "j_joker": 0.8,       # +4 mult
    }

    def _joker_strategy_value(
        self, joker: dict, strat: Strategy, owned_jokers: list[dict] | None = None,
    ) -> float:
        """Score how valuable a joker is to our current strategy.

        Uses parsed effect text to assess actual accumulated value for
        scaling jokers, instead of relying solely on the static tier list.

        When owned_jokers is provided, adds a coherence bonus for jokers
        that share strategic hand types with other owned jokers — making
        it much harder to sell a joker that's part of a cohesive build.
        """
        from joker_effects import JOKER_EFFECTS, _noop, parse_effect_value
        from strategy import JOKER_HAND_AFFINITY as STRAT_AFFINITY

        key = joker.get("key", "")
        effect = JOKER_EFFECTS.get(key)

        # No scoring effect at all — lowest value
        if effect is None or effect is _noop:
            return 0.0

        # Parse the actual current value from the joker's effect text
        effect_text = joker.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect_text) if effect_text else {}

        # Compute a dynamic power score from parsed values
        # xmult jokers are most valuable, then +mult, then +chips
        dynamic_power = 0.0
        if parsed.get("xmult") and parsed["xmult"] > 1.0:
            dynamic_power = parsed["xmult"] * 2.0  # X2.5 → 5.0, X4 → 8.0
        if parsed.get("mult") and parsed["mult"] > 0:
            dynamic_power = max(dynamic_power, parsed["mult"] / 5.0)  # +30 mult → 6.0, +5 → 1.0
        if parsed.get("chips") and parsed["chips"] > 0:
            dynamic_power = max(dynamic_power, parsed["chips"] / 50.0)  # +100 chips → 2.0

        # Unconditional jokers — use the higher of static tier or dynamic parsed value
        if key not in STRAT_AFFINITY:
            static_power = self.UNCONDITIONAL_POWER.get(key, 1.0)
            return max(static_power, dynamic_power)

        # Conditional joker — value depends on strategy alignment + dynamic power
        hand_types, weight = STRAT_AFFINITY[key]
        synergy = sum(strat.hand_affinity(ht) for ht in hand_types)

        # Build coherence bonus: count how many other owned jokers share
        # at least one hand type with this joker. A joker embedded in a
        # cohesive build (e.g. j_duo + j_jolly + j_sly all on Pair) is
        # worth far more than its individual score suggests.
        coherence_bonus = 0.0
        if owned_jokers and key in STRAT_AFFINITY:
            my_hands = set(STRAT_AFFINITY[key][0])
            allies = 0
            for other in owned_jokers:
                okey = other.get("key", "")
                if okey == key or okey not in STRAT_AFFINITY:
                    continue
                other_hands = set(STRAT_AFFINITY[okey][0])
                if my_hands & other_hands:
                    allies += 1
            coherence_bonus = allies * 1.5  # each ally adds 1.5 to sell resistance

        if synergy > 0:
            return max(2.0 + synergy + coherence_bonus, dynamic_power)
        return max(0.5 + coherence_bonus, dynamic_power)

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])

        # Proactive sell: Popcorn decayed to ≤4 mult — cash out before it disappears
        from joker_effects import parse_effect_value
        for i, j in enumerate(owned):
            if j.get("key") == "j_popcorn":
                effect_text = j.get("value", {}).get("effect", "")
                parsed = parse_effect_value(effect_text) if effect_text else {}
                current_mult = parsed.get("mult", 99)
                if current_mult <= 4:
                    return SellJoker(
                        i, reason=f"sell decayed Popcorn (+{current_mult} Mult, about to disappear)"
                    )

        # Proactive sell: Ramen decayed below X1.0 — it's now REDUCING scores
        for i, j in enumerate(owned):
            if j.get("key") == "j_ramen":
                effect_text = j.get("value", {}).get("effect", "")
                parsed = parse_effect_value(effect_text) if effect_text else {}
                current_xmult = parsed.get("xmult")
                if current_xmult is not None and current_xmult < 1.0:
                    return SellJoker(
                        i, reason=f"sell decayed Ramen (X{current_xmult:.2f}, reducing scores)"
                    )

        # Only consider selling when slots are full
        if joker_info.get("count", 0) < joker_info.get("limit", 5):
            return None

        if not owned:
            return None

        hand_levels = state.get("hands", {})
        strat = compute_strategy(owned, hand_levels)

        # Don't sell if we have no strategic direction yet
        if not strat.preferred_hands:
            return None

        # Score each owned joker — never sell scaling jokers, Madness, or
        # Ceremonial Dagger itself
        protected = {"j_madness", "j_ceremonial"} | SCALING_JOKERS
        owned_values = [
            (i, self._joker_strategy_value(j, strat, owned_jokers=owned), j)
            for i, j in enumerate(owned)
            if j.get("key") not in protected
        ]
        if not owned_values:
            return None

        # Find the weakest joker
        weakest_idx, weakest_value, weakest_joker = min(owned_values, key=lambda x: x[1])

        # Check if the shop has a better joker available
        shop = state.get("shop", {})
        money = state.get("money", 0)
        sell_value = weakest_joker.get("cost", {}).get("sell", 0)

        from joker_effects import JOKER_EFFECTS, _noop
        from strategy import JOKER_HAND_AFFINITY as STRAT_AFFINITY

        INTEREST_CAP = 25
        current_interest = min(money // 5, 5)
        ante = state.get("ante_num", 1)

        for card in shop.get("cards", []):
            if card.get("set") != "JOKER":
                continue
            cost = card.get("cost", {}).get("buy", 999)
            money_after_sell = money + sell_value
            if cost > money_after_sell:
                continue

            # Check that BuyJokersInShop won't block this buy due to interest
            # (mirrors its logic: skip if losing interest below cap, unless ante >= 5)
            if ante < 5 and money_after_sell < INTEREST_CAP:
                interest_after_buy = min((money_after_sell - cost) // 5, 5)
                if interest_after_buy < current_interest:
                    continue  # buy would be blocked by interest threshold

            # Score the shop joker WITHOUT coherence bonus (it has no allies yet)
            shop_value = self._joker_strategy_value(card, strat)

            # Dynamic threshold: much harder to justify selling on-strategy
            # jokers for off-strategy replacements
            weakest_key = weakest_joker.get("key", "")
            weakest_synergy = sum(
                strat.hand_affinity(ht)
                for ht in STRAT_AFFINITY.get(weakest_key, ([], 0))[0]
            )
            shop_key = card.get("key", "")
            shop_synergy = sum(
                strat.hand_affinity(ht)
                for ht in STRAT_AFFINITY.get(shop_key, ([], 0))[0]
            )

            threshold = 1.0  # base: shop joker must be 1.0 better
            if weakest_synergy > 0 and shop_synergy == 0:
                threshold = 3.0  # selling on-strategy for off-strategy: huge bar
            elif weakest_synergy > 0 and shop_synergy > 0:
                threshold = 1.5  # on-strategy swap: still needs clear improvement

            if shop_value > weakest_value + threshold:
                shop_label = card.get("label", "?")
                return SellJoker(
                    weakest_idx,
                    reason=f"sell {weakest_joker.get('label', '?')} (value={weakest_value:.1f}) "
                           f"for {shop_label} (value={shop_value:.1f}, threshold={threshold:.1f}) "
                           f"[strategy: {', '.join(n for n, _ in strat.preferred_hands[:2])}]",
                )

        return None


class FeedCampfire:
    """When Campfire is owned, sell consumables that feed its X0.25 Mult per sell.

    Campfire gains X0.25 Mult each time a joker, tarot, or planet is sold
    (deliberate sell only — destruction doesn't count). Resets at each boss blind.

    Sells:
    - Planets that level hands with no strategy affinity (we'd never play them)
    - Unrecognized/unusable consumables sitting in our slots

    Does NOT sell:
    - Black Hole (levels everything — always use)
    - Planets for our strategy hands (use them to level up)
    - Tarots/Spectrals we know how to use
    """
    name = "feed_campfire"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        owned_jokers = state.get("jokers", {}).get("cards", [])
        if not any(j.get("key") == "j_campfire" for j in owned_jokers):
            return None

        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_levels = state.get("hands", {})
        strat = compute_strategy(owned_jokers, hand_levels)

        all_useful = (
            SAFE_CONSUMABLE_TAROTS
            | set(TARGETING_TAROTS)
            | SAFE_SPECTRAL_CONSUMABLES
            | set(SPECTRAL_TARGETING)
        )

        for i, card in enumerate(consumables):
            key = card.get("key", "")

            # Planets: keep Black Hole and any that level a strategy hand
            if key in PLANET_KEYS:
                hand_type = PLANET_KEYS[key]
                if hand_type == "ALL":
                    continue  # Black Hole — always use, never sell
                if strat.hand_affinity(hand_type) > 0:
                    continue  # Levels a hand we care about — use it
                return SellConsumable(
                    i,
                    reason=f"Campfire: sell {card.get('label', '?')} (+X0.25 Mult, {hand_type} has no affinity)",
                )

            # Hex: sell if it would destroy our lineup (same gates as UseImmediateConsumables)
            if key == "c_hex":
                joker_count = state.get("jokers", {}).get("count", 0)
                joker_limit = state.get("jokers", {}).get("limit", 5)
                owned_keys = {j.get("key") for j in owned_jokers}
                ante = state.get("ante_num", 1)
                if joker_count >= joker_limit or owned_keys & SCALING_JOKERS or ante >= 5:
                    return SellConsumable(
                        i, reason=f"Campfire: sell Hex (+X0.25 Mult, would destroy lineup)",
                    )

            # Tarots/Spectrals we know how to use — keep them
            if key in all_useful:
                continue

            # Unknown or unusable consumable — sell for Campfire
            return SellConsumable(
                i,
                reason=f"Campfire: sell {card.get('label', '?')} (+X0.25 Mult)",
            )

        return None


class ReorderJokersForCeremonial:
    """When Ceremonial Dagger is owned, arrange jokers so only fodder gets eaten.

    Ceremonial Dagger destroys the joker immediately to its right at the start
    of each blind, gaining permanent mult. Layout:
      [valuable jokers] [Ceremonial Dagger] [fodder OR nothing]

    Fodder = jokers with no scoring effect (_noop) or very low strategy value.
    If no fodder exists, Ceremonial goes rightmost and eats nothing.
    """
    name = "reorder_jokers_for_ceremonial"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        # Amber Acorn reshuffles jokers every hand — reordering is immediately undone
        if ctx.blind_name == "Amber Acorn":
            return None

        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])
        if len(owned) < 2:
            return None

        ceremonial_idx = next(
            (i for i, j in enumerate(owned) if j.get("key") == "j_ceremonial"), None
        )
        if ceremonial_idx is None:
            return None

        from joker_effects import JOKER_EFFECTS, _noop

        # Classify each non-Ceremonial joker as valuable or fodder
        # Fodder: no scoring effect, or economy-only jokers
        valuable = []
        fodder = []
        for i, j in enumerate(owned):
            if j.get("key") == "j_ceremonial":
                continue
            key = j.get("key", "")
            effect = JOKER_EFFECTS.get(key)
            # Protected jokers are always valuable
            if key in ({"j_madness"} | SCALING_JOKERS):
                valuable.append(i)
            elif effect is None or effect is _noop:
                fodder.append(i)  # no scoring effect — safe to sacrifice
            else:
                valuable.append(i)  # has a scoring effect — keep it

        # Desired layout: [valuable] [ceremonial] [one fodder if available]
        if fodder:
            desired_order = valuable + [ceremonial_idx] + [fodder[0]]
            # Remaining fodder (if multiple) go before ceremonial
            desired_order = valuable + fodder[1:] + [ceremonial_idx] + [fodder[0]]
        else:
            # No fodder — park Ceremonial rightmost, eats nothing
            desired_order = valuable + [ceremonial_idx]

        current_order = list(range(len(owned)))
        if desired_order == current_order:
            return None

        fodder_label = owned[fodder[0]].get("label", "?") if fodder else "none"
        return RearrangeJokers(
            order=desired_order,
            reason=f"reorder for Ceremonial Dagger: fodder={fodder_label}",
        )


class BuyJokersInShop:
    """Buy jokers that improve scoring, respecting interest thresholds."""
    name = "buy_jokers_in_shop"

    INTEREST_CAP = 25
    # Minimum score improvement (%) to justify a purchase that loses interest
    MIN_IMPROVEMENT = 0.10

    # Jokers worth buying regardless of money — these are run-defining.
    # xmult jokers and strong unconditional mult/chips.
    ALWAYS_BUY = {
        # Unconditional xmult
        "j_cavendish",      # X3
        "j_stencil",        # X1 per empty slot
        # Hand-type xmult
        "j_duo",            # X2 on Pair
        "j_trio",           # X3 on Three of a Kind
        "j_family",         # X4 on Four of a Kind
        "j_order",          # X3 on Straight
        "j_tribe",          # X2 on Flush
        # Strong unconditional
        "j_gros_michel",    # +15 mult
        "j_popcorn",        # +20 mult (decays but huge early)
        # Strong conditional xmult
        "j_acrobat",        # X3 on final hand
        "j_blackboard",     # X3 if held cards spades/clubs
        "j_flower_pot",     # X3 if all 4 suits
        # Scaling xmult
        "j_madness",        # +X0.5 per blind, eats a joker (needs fodder)
    }

    # Primary scoring category for each joker — used for build composition weighting.
    # Jokers not listed here (utility, economy, copy) return a neutral 1.0 multiplier.
    JOKER_SCORE_CATEGORY: dict[str, set[str]] = {
        "xmult": {
            # Unconditional
            "j_cavendish", "j_stencil",
            # Hand-type
            "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
            # Card property
            "j_photograph", "j_baron", "j_bloodstone", "j_triboulet",
            # Game-state conditional
            "j_blackboard", "j_acrobat", "j_flower_pot", "j_seeing_double",
            "j_steel_joker", "j_loyalty_card", "j_drivers_license",
            # Scaling
            "j_madness", "j_vampire", "j_hologram", "j_obelisk",
            "j_lucky_cat", "j_glass", "j_campfire", "j_throwback",
            "j_card_sharp", "j_ancient", "j_baseball", "j_canio",
            "j_yorick", "j_hit_the_road", "j_constellation", "j_idol",
        },
        "mult": {
            # Unconditional
            "j_joker", "j_misprint", "j_gros_michel", "j_popcorn",
            # Hand-type
            "j_jolly", "j_zany", "j_mad", "j_crazy", "j_droll",
            # Suit conditional
            "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
            "j_onyx_agate",
            # Card property
            "j_smiley", "j_fibonacci", "j_even_steven",
            "j_shoot_the_moon", "j_raised_fist",
            # Game-state conditional
            "j_half", "j_abstract", "j_mystic_summit", "j_bootstraps",
            "j_swashbuckler", "j_erosion",
            # Scaling
            "j_ceremonial", "j_supernova", "j_ride_the_bus", "j_green_joker",
            "j_red_card", "j_flash", "j_fortune_teller", "j_trousers", "j_ramen",
        },
        "chips": {
            # Unconditional
            "j_blue_joker", "j_stuntman", "j_ice_cream",
            # Hand-type
            "j_sly", "j_wily", "j_clever", "j_devious", "j_crafty",
            # Suit conditional
            "j_arrowhead",
            # Card property
            "j_scary_face", "j_odd_todd",
            # Game-state conditional
            "j_banner", "j_bull",
            # Scaling
            "j_runner", "j_square", "j_castle", "j_wee", "j_hiker", "j_stone",
        },
    }

    def _composition_multiplier(self, owned_jokers: list, candidate_key: str) -> float:
        """Weight candidate by how much it fills a scoring gap in the current build.

        Returns >1.0 if candidate fills an underrepresented category,
        <1.0 if it stacks an already-full category, 1.0 if neutral/utility.

        Formula: need(category) = 1/(1+count), normalized against the average
        need across all three categories. A perfectly balanced build (1/1/1) yields
        1.0 for all candidates. A build with 0 xmult and 2 mult/chips yields ~1.5
        for xmult candidates and ~0.75 for mult/chips candidates.
        """
        candidate_cat = None
        for cat, keys in self.JOKER_SCORE_CATEGORY.items():
            if candidate_key in keys:
                candidate_cat = cat
                break

        if candidate_cat is None:
            return 1.0  # utility/economy/copy jokers — neutral

        counts: dict[str, int] = {"xmult": 0, "mult": 0, "chips": 0}
        for j in owned_jokers:
            k = j.get("key", "")
            for cat, keys in self.JOKER_SCORE_CATEGORY.items():
                if k in keys:
                    counts[cat] += 1
                    break

        needs = {cat: 1.0 / (1.0 + counts[cat]) for cat in counts}
        avg_need = sum(needs.values()) / len(needs)

        if avg_need == 0:
            return 1.0

        raw = needs[candidate_cat] / avg_need
        return max(0.5, min(2.0, raw))

    def _interest_after(self, money: int, cost: int) -> int:
        return min((money - cost) // 5, 5)

    def _has_scoring_effect(self, joker_key: str) -> bool:
        """Check if a joker has a real scoring effect (not a no-op)."""
        from joker_effects import JOKER_EFFECTS, _noop
        effect = JOKER_EFFECTS.get(joker_key)
        return effect is not None and effect is not _noop

    def _score_improvement(self, state: dict, candidate_joker: dict) -> float:
        """Estimate how much a new joker improves our best hand score.

        Returns fractional improvement (0.5 = 50% better).
        If no hand is available (e.g. in shop), returns a positive value
        for jokers with known scoring effects so they still get bought.
        """
        hand_cards = state.get("hand", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        current_jokers = state.get("jokers", {}).get("cards", [])

        if not hand_cards:
            # No hand to evaluate — use strategy to score the joker
            key = candidate_joker.get("key", "")
            if not self._has_scoring_effect(key):
                return 0.0

            strat = compute_strategy(current_jokers, state.get("hands", {}))
            from strategy import JOKER_HAND_AFFINITY as STRAT_AFFINITY

            # Joker that boosts our preferred hand type is worth more
            if key in STRAT_AFFINITY:
                hand_types, weight = STRAT_AFFINITY[key]
                synergy = sum(strat.hand_affinity(ht) for ht in hand_types)
                if synergy > 0:
                    return 0.40  # strong synergy
                return 0.15  # has effect but doesn't synergize

            return 0.20  # unconditional joker, always decent

        joker_limit = state.get("jokers", {}).get("limit", 5)
        current_best = best_hand(hand_cards, hand_levels, jokers=current_jokers, joker_limit=joker_limit)
        with_new = best_hand(hand_cards, hand_levels, jokers=current_jokers + [candidate_joker], joker_limit=joker_limit)

        if not current_best or not with_new:
            return 0.0

        if current_best.total == 0:
            return 1.0 if with_new.total > 0 else 0.0

        return (with_new.total - current_best.total) / current_best.total

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        shop = state.get("shop", {})
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)

        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            return None

        current_interest = min(money // 5, 5)

        # Score each candidate and pick the best improvement
        best_idx = None
        best_improvement = 0.0
        best_cost = 0
        best_label = ""
        passed_on: list[str] = []

        for i, card in enumerate(shop.get("cards", [])):
            if card.get("set") != "JOKER":
                continue
            label = card.get("label", "?")
            cost = card.get("cost", {}).get("buy", 999)
            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            key = card.get("key", "")
            joker_count = joker_slots.get("count", 0)

            # S-tier jokers: buy immediately, ignore interest thresholds
            # Exception: don't buy Madness if we own a scaling joker it could eat
            if key == "j_madness":
                owned_keys = {j.get("key") for j in joker_slots.get("cards", [])}
                if owned_keys & SCALING_JOKERS:
                    passed_on.append(f"{label}(${cost}, would eat scaling joker)")
                    continue
            # Don't buy scaling jokers if Madness is owned — it'll eat them
            if key in SCALING_JOKERS:
                owned_keys = {j.get("key") for j in joker_slots.get("cards", [])}
                if "j_madness" in owned_keys:
                    passed_on.append(f"{label}(${cost}, Madness would eat it)")
                    continue
            # Slow scaling jokers (start at X1.0, grow over many rounds) are
            # worthless late game — they won't compound fast enough to matter.
            # Ceremonial Dagger also needs many rounds of fodder to pay off.
            SLOW_SCALERS = SCALING_JOKERS | {"j_madness", "j_ceremonial"}
            if key in SLOW_SCALERS and ante >= 6:
                passed_on.append(f"{label}(${cost}, too late for slow scaler at ante {ante})")
                continue
            if key in self.ALWAYS_BUY:
                improvement = 1.0  # force-buy
            # Stencil restriction: filling slots reduces its ×mult
            elif any(j.get("key") == "j_stencil" for j in joker_slots.get("cards", [])):
                joker_limit = joker_slots.get("limit", 5)
                # After buying: empty slots = (limit - count - 1), +1 for Stencil counting itself
                stencil_mult_after = (joker_limit - joker_count - 1) + 1
                if stencil_mult_after <= 2:
                    # Buying would leave Stencil at ×1 or ×2 — need a very strong joker
                    improvement = self._score_improvement(state, card)
                    if improvement < 0.40:
                        passed_on.append(f"{label}(${cost}, Stencil restriction: only ×{stencil_mult_after} left)")
                        continue
                else:
                    improvement = self._score_improvement(state, card)
            # First joker: buy anything — 0 jokers is a death sentence
            elif joker_count == 0:
                improvement = 0.50
            # Joker-starved: ≤2 jokers means building a scoring engine beats saving interest
            elif joker_count <= 2:
                improvement = self._score_improvement(state, card)
            # Early game (ante ≤ 2): always prioritize jokers over saving interest
            elif state.get("ante_num", 1) <= 2:
                improvement = self._score_improvement(state, card)
            # Late game (ante ≥ 5): interest won't matter, just score the joker
            elif state.get("ante_num", 1) >= 5:
                improvement = self._score_improvement(state, card)
            else:
                interest_after = self._interest_after(money, cost)
                loses_interest = interest_after < current_interest

                if loses_interest:
                    if money >= self.INTEREST_CAP:
                        improvement = self._score_improvement(state, card)
                        if improvement < self.MIN_IMPROVEMENT:
                            passed_on.append(f"{label}(${cost}, +{improvement:.0%} below threshold)")
                            continue
                    else:
                        if cost > 2:
                            passed_on.append(f"{label}(${cost}, saving for interest)")
                            continue
                        improvement = self._score_improvement(state, card)
                else:
                    improvement = self._score_improvement(state, card)

            # Weight by build composition: fill gaps, don't stack full categories
            improvement *= self._composition_multiplier(joker_slots.get("cards", []), key)

            if improvement > best_improvement:
                best_improvement = improvement
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            return BuyCard(
                best_idx,
                reason=f"buy joker: {best_label} for ${best_cost} "
                       f"(+{best_improvement:.0%} score, ${money}->${money - best_cost})",
            )
        if passed_on:
            log.info("Passed on jokers: %s", ", ".join(passed_on))
        return None


class BuyConsumablesInShop:
    """Buy Planet cards and useful Tarots from the shop."""
    name = "buy_consumables_in_shop"

    INTEREST_CAP = 25

    # Planet card keys -> hand type they level (for strategy-aware buying)
    PLANET_KEYS = {
        "c_mercury": "Pair", "c_venus": "Three of a Kind", "c_earth": "Full House",
        "c_mars": "Four of a Kind", "c_jupiter": "Flush", "c_saturn": "Straight",
        "c_uranus": "Two Pair", "c_neptune": "Straight Flush", "c_pluto": "High Card",
        "c_planet_x": "Five of a Kind", "c_ceres": "Flush House", "c_eris": "Flush Five",
        "c_black_hole": "ALL",
    }

    # No-target Tarots worth buying from shop
    GOOD_TAROTS = {
        "c_judgement",         # creates random Joker
        "c_high_priestess",   # creates 2 random Planets
        "c_hermit",           # doubles money (max $20)
        "c_emperor",          # creates 2 random Tarots
    }

    # Targeting Tarots worth buying — Glass and suit conversions are high value
    GOOD_TARGETING_TAROTS = {
        "c_justice": 3,    # Glass — ×2 mult on face cards, very strong
        "c_star": 4,       # Suit conversions — strategy-dependent
        "c_moon": 4,
        "c_sun": 4,
        "c_world": 4,
        "c_lovers": 4,     # Enhancements — always useful
        "c_chariot": 4,
        "c_hierophant": 4,
        "c_empress": 4,
        "c_magician": 4,
        "c_devil": 4,
        "c_strength": 5,   # Rank up — situational
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        shop = state.get("shop", {})
        consumables = state.get("consumables", {})

        # Need room in consumable slots
        if consumables.get("count", 0) >= consumables.get("limit", 2):
            return None

        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)

        best_idx = None
        best_priority = 999
        best_cost = 0
        best_label = ""
        passed_on: list[str] = []

        for i, card in enumerate(shop.get("cards", [])):
            key = card.get("key", "")
            label = card.get("label", "?")
            card_set = card.get("set", "")
            cost = card.get("cost", {}).get("buy", 999)

            # Planet cards — on-strategy priority 1, off-strategy skip
            if key in self.PLANET_KEYS or card_set == "PLANET":
                hand_type = self.PLANET_KEYS.get(key)
                if hand_type == "ALL":
                    priority = 1  # Black Hole — always buy
                elif hand_type and strat.hand_affinity(hand_type) > 0:
                    priority = 1  # levels a hand we care about
                else:
                    passed_on.append(f"{label}({hand_type}, off-strategy)")
                    continue
            # Good no-target Tarots — priority 2
            elif key in self.GOOD_TAROTS:
                priority = 2
            # Good targeting Tarots — priority 3-5
            elif key in self.GOOD_TARGETING_TAROTS:
                priority = self.GOOD_TARGETING_TAROTS[key]
            else:
                if card_set in ("TAROT", "PLANET", "SPECTRAL"):
                    passed_on.append(f"{label}(not in buy list)")
                continue

            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            # Respect interest below cap (planets are cheap, usually $3-4)
            current_interest = min(money // 5, 5)
            if money < self.INTEREST_CAP:
                interest_after = min((money - cost) // 5, 5)
                if interest_after < current_interest and cost > 3:
                    passed_on.append(f"{label}(${cost}, saving for interest)")
                    continue

            if priority < best_priority or (priority == best_priority and cost < best_cost):
                best_priority = priority
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            return BuyCard(
                best_idx,
                reason=f"buy consumable: {best_label} for ${best_cost} (${money}->${money - best_cost})",
            )
        if passed_on:
            log.info("Passed on consumables: %s", ", ".join(passed_on))
        return None


class BuyPacksInShop:
    """Buy packs from the shop, prioritizing Planet > Buffoon > Tarot."""
    name = "buy_packs_in_shop"

    INTEREST_CAP = 25

    # Pack type priority by label keyword. Lower = buy first.
    # Detected by checking if the keyword appears in the card label.
    PACK_PRIORITY = {
        "Celestial": 1,   # Planet packs
        "Buffoon": 2,     # Joker packs
        # Standard packs intentionally excluded — they dilute the deck
        "Arcana": 3,      # Tarot packs
    }
    # Standard and Spectral packs intentionally omitted

    def _pack_priority(self, label: str) -> int | None:
        for keyword, priority in self.PACK_PRIORITY.items():
            if keyword in label:
                return priority
        return None

    def _interest_after(self, money: int, cost: int) -> int:
        return min((money - cost) // 5, 5)

    # All pack keywords for Red Card buying (buy any pack just to skip it)
    ALL_PACK_KEYWORDS = {"Celestial", "Buffoon", "Arcana", "Standard", "Spectral"}

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        packs = state.get("packs", {})
        joker_slots = state.get("jokers", {})
        owned_jokers = joker_slots.get("cards", [])
        has_red_card = any(j.get("key") == "j_red_card" for j in owned_jokers)

        best_idx = None
        best_priority = 999
        best_cost = 0
        best_label = ""

        passed_on: list[str] = []

        for i, card in enumerate(packs.get("cards", [])):
            label = card.get("label", "")
            cost = card.get("cost", {}).get("buy", 999)
            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            # Red Card: buy ANY pack to skip it for +3 mult
            if has_red_card and any(kw in label for kw in self.ALL_PACK_KEYWORDS):
                if cost <= 4 or money >= self.INTEREST_CAP:
                    priority = 0
                else:
                    passed_on.append(f"{label}(${cost}, Red Card but too expensive)")
                    continue
            else:
                priority = self._pack_priority(label)
                if priority is None:
                    if "Spectral" in label and state.get("ante_num", 1) >= 3:
                        priority = 4
                    else:
                        passed_on.append(f"{label}(${cost}, not in buy list)")
                        continue

            # Skip Buffoon packs if joker slots are full
            if "Buffoon" in label and not has_red_card:
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    passed_on.append(f"{label}(${cost}, joker slots full)")
                    continue

            # Interest check — never drop below the next $5 threshold
            if not has_red_card:
                current_interest = min(money // 5, 5)
                interest_after = self._interest_after(money, cost)
                loses_interest = interest_after < current_interest
                if loses_interest:
                    if money >= self.INTEREST_CAP:
                        if "Celestial" not in label:
                            passed_on.append(f"{label}(${cost}, would lose interest)")
                            continue
                    else:
                        passed_on.append(f"{label}(${cost}, saving for interest)")
                        continue

            if priority < best_priority:
                best_priority = priority
                best_idx = i
                best_cost = cost
                best_label = label

        if best_idx is not None:
            reason = f"buy pack: {best_label} for ${best_cost} (${money}->${money - best_cost})"
            if has_red_card:
                reason += " [Red Card: +3 mult on skip]"
            return BuyPack(best_idx, reason=reason)
        if passed_on:
            log.info("Passed on packs: %s", ", ".join(passed_on))
        return None


class BuyVouchersInShop:
    """Buy high-impact vouchers when we can afford them."""
    name = "buy_vouchers_in_shop"

    INTEREST_CAP = 25

    # Voucher keys ranked by priority. Lower = buy first.
    # Only include vouchers with direct gameplay impact.
    VOUCHER_PRIORITY: dict[str, int] = {
        # Tier 1: extra scoring attempts
        "v_grabber": 1,          # +1 hand per round
        "v_nacho_tong": 1,       # +1 more hand
        # Tier 2: bigger hands = better poker hands
        "v_paint_brush": 2,      # +1 hand size
        "v_palette": 2,          # +1 more hand size
        # Tier 3: more discards for hand improvement
        "v_wasteful": 3,         # +1 discard
        "v_recyclomancy": 3,     # +1 more discard
        # Tier 4: more joker capacity
        "v_antimatter": 4,       # +1 joker slot (requires Blank bought)
        # Tier 5: consumable capacity
        "v_crystal_ball": 5,     # +1 consumable slot
        # Tier 6: ante reduction (powerful but trades resources)
        "v_hieroglyph": 6,       # -1 ante, -1 hand per round
        # Tier 7: economy (nice to have)
        "v_seed_money": 7,       # interest cap to $10/round
        "v_money_tree": 7,       # interest cap to $20/round
        "v_clearance_sale": 8,   # 25% off
        "v_overstock": 8,        # +1 shop card slot
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        vouchers = state.get("vouchers", {})

        # Only buy vouchers when we're at the interest cap with surplus
        # Vouchers cost $10 — don't dip below $25
        if money < self.INTEREST_CAP + 10:
            return None

        best_idx = None
        best_priority = 999
        best_cost = 0
        best_label = ""

        for i, card in enumerate(vouchers.get("cards", [])):
            key = card.get("key", "")
            priority = self.VOUCHER_PRIORITY.get(key)
            if priority is None:
                continue

            cost = card.get("cost", {}).get("buy", 999)
            if cost > money - self.INTEREST_CAP:
                # Don't spend below interest cap
                continue

            if priority < best_priority:
                best_priority = priority
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            return BuyVoucher(
                best_idx,
                reason=f"buy voucher: {best_label} for ${best_cost} (${money}->${money - best_cost})",
            )
        return None


class RerollShop:
    """Reroll the shop when we're flush with cash and nothing good is available."""
    name = "reroll_shop"

    # Only reroll above this money threshold (well above interest cap)
    MIN_MONEY_TO_REROLL = 35
    # Reroll costs $5 base (can be reduced by vouchers)
    REROLL_COST = 5
    # Max rerolls per shop visit to prevent infinite loops
    MAX_REROLLS = 3

    def __init__(self) -> None:
        self._rerolls_this_shop = 0
        self._last_round = -1

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        round_num = state.get("round_num", 0)

        # Reset counter on new shop visit
        if round_num != self._last_round:
            self._rerolls_this_shop = 0
            self._last_round = round_num

        if self._rerolls_this_shop >= self.MAX_REROLLS:
            return None

        # Need enough money that rerolling doesn't hurt our interest
        if money < self.MIN_MONEY_TO_REROLL:
            return None

        # Don't reroll if there's a good joker we should buy
        shop = state.get("shop", {})
        joker_slots = state.get("jokers", {})
        has_open_slots = joker_slots.get("count", 0) < joker_slots.get("limit", 5)

        if has_open_slots:
            # Check if any joker in shop has a scoring effect
            from joker_effects import JOKER_EFFECTS, _noop
            for card in shop.get("cards", []):
                if card.get("set") == "JOKER":
                    key = card.get("key", "")
                    effect = JOKER_EFFECTS.get(key)
                    if effect is not None and effect is not _noop:
                        cost = card.get("cost", {}).get("buy", 999)
                        if cost <= money:
                            return None  # good joker available, buy instead

        strat = compute_strategy(
            state.get("jokers", {}).get("cards", []), state.get("hands", {})
        )
        strat_str = ", ".join(n for n, _ in strat.preferred_hands[:2]) if strat.preferred_hands else "no strategy yet"
        self._rerolls_this_shop += 1
        return Reroll(reason=f"reroll shop (${money}, looking for {strat_str} jokers)")


class LeaveShop:
    """When done shopping, move to next round."""
    name = "leave_shop"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return NextRound(reason="done shopping")


# ---------------------------------------------------------------------------
# Rules: ROUND_EVAL
# ---------------------------------------------------------------------------

class AlwaysCashOut:
    name = "always_cash_out"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return CashOut(reason="cash out")


# ---------------------------------------------------------------------------
# Rules: Pack opened
# ---------------------------------------------------------------------------

# Tarot card data for pack picking
# No-target Tarots ranked by value (higher = better)
NO_TARGET_TAROTS: dict[str, int] = {
    "c_judgement": 6,         # creates random Joker
    "c_high_priestess": 5,    # creates 2 random Planets
    "c_hermit": 4,            # doubles money (max $20)
    "c_emperor": 3,           # creates 2 random Tarots
    "c_temperance": 2,        # money = joker sell values
    "c_wheel_of_fortune": 1,  # 1/4 edition on random joker
    "c_fool": 0,              # copy last used — situational
}

# Tarots that need targets: key -> (max_targets, effect_type, extra)
TARGETING_TAROTS: dict[str, tuple] = {
    # Enhancements
    "c_lovers":     (1, "enhance", "Wild"),
    "c_chariot":    (1, "enhance", "Steel"),
    "c_hierophant": (2, "enhance", "Bonus"),
    "c_empress":    (2, "enhance", "Mult"),
    "c_magician":   (2, "enhance", "Lucky"),
    "c_devil":      (1, "gold", None),       # $3 held — target junk card, use immediately
    # Suit conversion
    "c_star":   (3, "suit_convert", "D"),
    "c_moon":   (3, "suit_convert", "C"),
    "c_sun":    (3, "suit_convert", "H"),
    "c_world":  (3, "suit_convert", "S"),
    # Rank manipulation
    "c_strength": (2, "rank_up", None),
    # Glass — ×2 mult on scored, 1/4 shatter risk. High value on face cards.
    "c_justice": (1, "glass", None),
    # Stone — +50 chips, removes rank/suit. Use on junk cards.
    "c_tower": (1, "stone", None),
    # Destroy — thins deck for more consistent draws
    "c_hanged_man": (2, "destroy", None),
    # Clone — left card becomes copy of right card
    "c_death": (2, "clone", None),
}

# ---------------------------------------------------------------------------
# Tarot target selection helpers (used by both pack picking and consumable usage)
# ---------------------------------------------------------------------------

FACE_RANKS_TAROT = {"J", "Q", "K"}


def _find_gold_targets(hand_cards: list[dict], count: int, current_best=None) -> list[int]:
    """Pick lowest-value non-enhanced card outside the best hand — junk we'd hold, not play."""
    from hand_evaluator import card_rank, rank_value, _modifier, is_debuffed
    best_indices = set(current_best.card_indices) if current_best else set()
    candidates = []
    for i, c in enumerate(hand_cards):
        if i in best_indices:
            continue  # don't gold a card we're about to play
        if is_debuffed(c):
            continue
        if _modifier(c).get("enhancement"):
            continue  # already enhanced
        r = card_rank(c)
        candidates.append((i, rank_value(r) if r else 0))
    candidates.sort(key=lambda x: x[1])  # lowest value first — best junk target
    return [i for i, _ in candidates[:count]]


def _find_enhancement_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Pick highest-rank non-enhanced cards as enhancement targets."""
    from hand_evaluator import card_rank, rank_value, _modifier
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_suit_convert_targets(
    hand_cards: list[dict], target_suit: str, count: int,
) -> list[int]:
    """Pick cards that are NOT the target suit (to convert them)."""
    from hand_evaluator import card_suit
    candidates = []
    for i, c in enumerate(hand_cards):
        s = card_suit(c)
        if s and s != target_suit:
            candidates.append(i)
    return candidates[:count]


def _find_rank_up_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Pick lowest-rank cards (rank +1 most impactful on low cards)."""
    from hand_evaluator import card_rank, rank_value
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_glass_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Face cards first — Glass ×2 stacks with face card jokers."""
    from hand_evaluator import card_rank, rank_value, _modifier
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            is_face = r in FACE_RANKS_TAROT
            # Sort face cards first (0), then by rank descending
            candidates.append((i, 0 if is_face else 1, -rank_value(r)))
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def _find_stone_targets(hand_cards: list[dict], count: int, jokers: list[dict]) -> list[int]:
    """Lowest-rank cards for Stone conversion. Only if card is junk (2-4) or we have Stone Joker."""
    from hand_evaluator import card_rank, rank_value, _modifier
    has_stone_joker = any(j.get("key") == "j_stone" for j in jokers)
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            rv = rank_value(r)
            # Only target truly junk cards (2-4) unless we have Stone Joker
            if has_stone_joker or rv <= 4:
                candidates.append((i, rv))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_destroy_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Lowest-rank non-enhanced cards for deck thinning."""
    from hand_evaluator import card_rank, rank_value, _modifier
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_clone_targets(hand_cards: list[dict], strategy=None) -> list[int] | None:
    """Death: left card becomes copy of right card (by hand position).

    Returns [sacrifice_idx, source_idx] where sacrifice_idx < source_idx,
    or None if no valid pair. Considers enhancements, seals, editions,
    and suit affinity — not just raw rank.
    """
    from hand_evaluator import card_rank, rank_value, card_suit, _modifier, is_debuffed
    scored = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if not r or is_debuffed(c):
            continue
        score = rank_value(r)
        mod = _modifier(c)
        enh = mod.get("enhancement")
        if enh == "STEEL":
            score += 15
        elif enh == "GLASS":
            score += 10
        elif enh in ("MULT", "LUCKY"):
            score += 5
        elif enh == "WILD":
            score += 4
        elif enh == "BONUS":
            score += 3
        elif enh == "GOLD":
            score += 2
        if mod.get("seal"):
            score += 8
        edition = mod.get("edition")
        if edition == "POLYCHROME":
            score += 20
        elif edition == "HOLOGRAPHIC":
            score += 10
        elif edition == "FOIL":
            score += 5
        if strategy:
            s = card_suit(c)
            if s:
                score += strategy.suit_affinity(s) * 3
        scored.append((i, score))
    if len(scored) < 2:
        return None
    scored.sort(key=lambda x: x[1])
    # Need sacrifice (left = lower index) and source (right = higher index).
    # Balatro's Death card transforms left into right by hand position.
    # Try best pair first: worst card sacrificed, best card cloned.
    worst_idx, worst_sc = scored[0]
    best_idx, best_sc = scored[-1]
    if best_sc - worst_sc < 4:
        return None
    if worst_idx < best_idx:
        return [worst_idx, best_idx]
    # Worst is to the RIGHT of best — can't use directly or we'd clone backwards.
    # Find an alternative pair that respects position ordering.
    # Option A: different sacrifice card that's left of best
    for idx, sc in scored:
        if idx < best_idx and best_sc - sc >= 4:
            return [idx, best_idx]
    # Option B: different source card that's right of worst
    for idx, sc in reversed(scored):
        if idx > worst_idx and sc - worst_sc >= 4:
            return [worst_idx, idx]
    return None


def _find_seal_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Highest-rank card in hand without a seal."""
    from hand_evaluator import card_rank, rank_value, _modifier
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("seal"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_edition_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Highest-rank card in hand without an edition."""
    from hand_evaluator import card_rank, rank_value, _modifier
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("edition"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_deck_enhance_targets(hand_cards: list[dict], count: int) -> list[int]:
    """Lowest-rank non-enhanced card — gets destroyed, replaced by better cards."""
    from hand_evaluator import card_rank, rank_value, _modifier
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_clone_deck_targets(
    hand_cards: list[dict], count: int, current_best: Any,
) -> list[int]:
    """Highest-rank scoring card in the current best hand — best candidate to duplicate."""
    from hand_evaluator import card_rank, rank_value
    if not current_best:
        return []
    scoring_set = set(current_best.card_indices)
    candidates = []
    for i, c in enumerate(hand_cards):
        if i not in scoring_set:
            continue
        r = card_rank(c)
        if r:
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_tarot_targets(
    effect_type: str, extra: str | None, max_count: int,
    hand_cards: list[dict], jokers: list[dict], strat: Strategy,
    current_best: Any = None,
) -> tuple[list[int] | None, float]:
    """Find targets for a targeting Tarot/Spectral. Returns (targets, score) or (None, 0)."""
    if effect_type == "gold":
        # Gold gives $3 when held — target the lowest junk card we won't play
        targets = _find_gold_targets(hand_cards, max_count, current_best)
        return (targets, 4.0) if targets else (None, 0)

    if effect_type == "enhance":
        targets = _find_enhancement_targets(hand_cards, max_count)
        return (targets, 3.0) if targets else (None, 0)

    if effect_type == "suit_convert":
        if not extra or strat.suit_affinity(extra) <= 0:
            return (None, 0)
        targets = _find_suit_convert_targets(hand_cards, extra, max_count)
        return (targets, 2.0 + strat.suit_affinity(extra)) if targets else (None, 0)

    if effect_type == "rank_up":
        targets = _find_rank_up_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)

    if effect_type == "glass":
        targets = _find_glass_targets(hand_cards, max_count)
        return (targets, 4.0) if targets else (None, 0)

    if effect_type == "stone":
        targets = _find_stone_targets(hand_cards, max_count, jokers)
        return (targets, 1.5) if targets else (None, 0)

    if effect_type == "destroy":
        targets = _find_destroy_targets(hand_cards, max_count)
        return (targets, 1.0) if targets else (None, 0)

    if effect_type == "clone":
        targets = _find_clone_targets(hand_cards, strat)
        return (targets, 3.5) if targets else (None, 0)

    if effect_type == "deck_enhance":
        targets = _find_deck_enhance_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)

    if effect_type == "seal":
        targets = _find_seal_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)

    if effect_type == "edition":
        targets = _find_edition_targets(hand_cards, max_count)
        return (targets, 1.0) if targets else (None, 0)

    if effect_type == "clone_deck":
        targets = _find_clone_deck_targets(hand_cards, max_count, current_best)
        return (targets, 1.5) if targets else (None, 0)

    return (None, 0)


class SkipPackForRedCard:
    """Skip packs to trigger Red Card's +3 mult scaling.

    Skips Standard, Spectral, and Arcana packs. Does NOT skip Celestial
    (planet cards too valuable) or Buffoon (free jokers too valuable).
    """
    name = "skip_pack_for_red_card"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        jokers = state.get("jokers", {}).get("cards", [])
        if not any(j.get("key") == "j_red_card" for j in jokers):
            return None

        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Don't skip if pack has Planets or Jokers — those are too valuable
        for c in cards:
            if c.get("set") == "PLANET" or c.get("label", "") in PLANET_HAND_MAP:
                return None
            if c.get("set") == "JOKER":
                return None
            if c.get("key", "") == "c_black_hole":
                return None

        return PackAction(card_index=None, reason="skip pack for Red Card (+3 mult)")


class PickFromTarotPack:
    """Pick the best Tarot card from an Arcana pack, with proper targeting."""
    name = "pick_from_tarot_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Only handle Tarot packs
        known_keys = set(NO_TARGET_TAROTS) | set(TARGETING_TAROTS)
        if not any(c.get("key", "") in known_keys for c in cards):
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)

        # Phase 1: Score no-target Tarots
        best_no_target_idx = None
        best_no_target_score = -1.0
        for i, card in enumerate(cards):
            key = card.get("key", "")
            if key in NO_TARGET_TAROTS:
                score = float(NO_TARGET_TAROTS[key])
                if score > best_no_target_score:
                    best_no_target_score = score
                    best_no_target_idx = i

        # Phase 2: Score targeting Tarots
        best_target_idx = None
        best_target_score = -1.0
        best_targets: list[int] = []

        if hand_cards:
            for i, card in enumerate(cards):
                key = card.get("key", "")
                if key not in TARGETING_TAROTS:
                    continue
                max_count, effect_type, extra = TARGETING_TAROTS[key]
                targets, score = _find_tarot_targets(
                    effect_type, extra, max_count, hand_cards, jokers, strat,
                )
                if targets and score > best_target_score:
                    best_target_score = score
                    best_target_idx = i
                    best_targets = targets

        # Pick the higher-scoring option
        if best_no_target_idx is not None and best_no_target_score >= best_target_score:
            label = cards[best_no_target_idx].get("label", "?")
            return PackAction(card_index=best_no_target_idx, reason=f"tarot: {label} (no target)")

        if best_target_idx is not None and best_targets:
            label = cards[best_target_idx].get("label", "?")
            return PackAction(
                card_index=best_target_idx,
                targets=best_targets,
                reason=f"tarot: {label} -> targets {best_targets}",
            )

        return PackAction(card_index=None, reason="skip tarot pack (nothing usable)")


# Planet card label -> hand type they level up
PLANET_HAND_MAP: dict[str, str] = {
    "Mercury": "Pair",
    "Venus": "Three of a Kind",
    "Earth": "Full House",
    "Mars": "Four of a Kind",
    "Jupiter": "Flush",
    "Saturn": "Straight",
    "Uranus": "Two Pair",
    "Neptune": "Straight Flush",
    "Pluto": "High Card",
    "Planet X": "Five of a Kind",
    "Ceres": "Flush House",
    "Eris": "Flush Five",
}

class PickFromPlanetPack:
    """Pick the planet card that best synergizes with our strategy."""
    name = "pick_from_planet_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Only handle Planet packs — check if any card is a known planet or Black Hole
        is_planet_pack = any(
            c.get("label", "") in PLANET_HAND_MAP
            or c.get("key", "") in ("c_black_hole",)
            or c.get("label", "") == "Black Hole"
            for c in cards
        )
        if not is_planet_pack:
            return None

        # Black Hole levels ALL hand types — always pick it first
        for i, card in enumerate(cards):
            if card.get("key", "") == "c_black_hole" or card.get("label", "") == "Black Hole":
                return PackAction(card_index=i, reason=f"planet: Black Hole (levels ALL hand types!)")

        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)

        # Balance of playability (how often you get this hand) and scaling
        # potential (how well the base chips×mult grows with levels).
        # Pair is common but has a low ceiling. Flush/Straight are less
        # common but scale much better — leveling them pays off more.
        HAND_VALUE: dict[str, float] = {
            "Two Pair": 8, "Pair": 7,          # most common hands — best default
            "Full House": 6, "Flush": 6, "Straight": 6,
            "Three of a Kind": 5,
            "High Card": 4, "Four of a Kind": 4,
            "Straight Flush": 2, "Five of a Kind": 2,
            "Flush House": 2, "Flush Five": 2,
        }

        best_idx = 0
        best_score = -1.0

        for i, card in enumerate(cards):
            label = card.get("label", "")
            hand_type = PLANET_HAND_MAP.get(label)
            if not hand_type:
                continue

            # Base score: balance of playability and scaling potential
            score = HAND_VALUE.get(hand_type, 1.0)

            # Strategy is the dominant factor — affinity from jokers
            affinity = strat.hand_affinity(hand_type)
            if affinity > 0:
                score += affinity * 10

            # Level bonus: compound growth on already-leveled types
            level_info = hand_levels.get(hand_type, {})
            current_level = level_info.get("level", 1)
            if current_level > 1:
                score *= 1.2 ** (current_level - 1)

            if score > best_score:
                best_score = score
                best_idx = i

        label = cards[best_idx].get("label", "?")
        hand_type = PLANET_HAND_MAP.get(label, "?")
        affinity = strat.hand_affinity(hand_type)

        # Log planets not chosen
        passed = []
        for i, card in enumerate(cards):
            if i == best_idx:
                continue
            cl = card.get("label", "?")
            cht = PLANET_HAND_MAP.get(cl)
            if cht:
                ca = strat.hand_affinity(cht)
                passed.append(f"{cl}({cht}, aff={ca:.0f})")
        if passed:
            log.info("Passed on planets: %s", ", ".join(passed))

        return PackAction(
            card_index=best_idx,
            reason=f"planet: {label} (levels {hand_type}, affinity={affinity:.0f})",
        )


class PickFromBuffoonPack:
    """Pick the joker with the best scoring effect from a Buffoon pack."""
    name = "pick_from_buffoon_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        joker_slots = state.get("jokers", {})

        if not cards:
            return None

        # Only handle Joker/Buffoon packs
        if not any(c.get("set") == "JOKER" for c in cards):
            return None

        # Can't pick jokers with full slots — skip the pack
        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            return PackAction(card_index=None, reason="skip buffoon pack (joker slots full)")

        from joker_effects import JOKER_EFFECTS, _noop, parse_effect_value
        from strategy import JOKER_HAND_AFFINITY as STRAT_AFFINITY

        owned_jokers = joker_slots.get("cards", [])
        hand_levels = state.get("hands", {})
        strat = compute_strategy(owned_jokers, hand_levels)

        best_idx = 0
        best_score = -1.0

        for i, card in enumerate(cards):
            key = card.get("key", "")
            effect = JOKER_EFFECTS.get(key)
            has_effect = effect is not None and effect is not _noop

            if not has_effect:
                # No-op jokers get a small baseline so they lose to any real effect
                score = 0.1
            else:
                # Score based on strategy synergy + effect strength
                score = 1.0

                # Parse actual values from effect text
                effect_text = card.get("value", {}).get("effect", "")
                parsed = parse_effect_value(effect_text) if effect_text else {}
                if parsed.get("xmult") and parsed["xmult"] > 1.0:
                    score = max(score, parsed["xmult"] * 2.0)
                if parsed.get("mult") and parsed["mult"] > 0:
                    score = max(score, parsed["mult"] / 5.0)
                if parsed.get("chips") and parsed["chips"] > 0:
                    score = max(score, parsed["chips"] / 50.0)

                # Strategy synergy bonus
                if key in STRAT_AFFINITY:
                    hand_types, weight = STRAT_AFFINITY[key]
                    synergy = sum(strat.hand_affinity(ht) for ht in hand_types)
                    if synergy > 0:
                        score += synergy * 2.0

                # S-tier jokers get a massive boost
                if key in BuyJokersInShop.ALWAYS_BUY:
                    score += 10.0

            if score > best_score:
                best_score = score
                best_idx = i

        label = cards[best_idx].get("label", "?")
        return PackAction(card_index=best_idx, reason=f"buffoon pick: {label} (score={best_score:.1f})")


class PickFromSpectralPack:
    """Pick the best Spectral card from a Spectral pack."""
    name = "pick_from_spectral_pack"

    # Base scores for each Spectral card; conditions applied at runtime
    SPECTRAL_SCORES: dict[str, float] = {
        "c_ectoplasm": 4.5,   # ×1 xmult on joker — massive, but -1 hand size
        "c_ankh":      4.0,   # clone a random joker
        "c_hex":       3.5,   # polychrome on joker (destroys other consumables)
        "c_wraith":    3.0,   # create random Rare joker, sets money $0
        "c_immolate":  2.5,   # destroy 5 cards, gain $20
        "c_familiar":  2.0,   # destroy 1, add 3 enhanced face cards
        "c_grim":      2.0,   # destroy 1, add 2 enhanced aces
        "c_incantation":2.0,  # destroy 1, add 4 enhanced numbers
        "c_deja_vu":   2.0,   # Red Seal (replays card)
        "c_trance":    2.0,   # Blue Seal (planet when held)
        "c_cryptid":   1.5,   # 2 copies of card in deck
        "c_aura":      1.0,   # random edition on a card
        "c_talisman":  1.0,   # Gold Seal ($3 when played)
        "c_medium":    1.0,   # Purple Seal (tarot when discarded)
        "c_sigil":     0.0,   # random suit conversion — skip
        "c_ouija":     0.0,   # permanent -1 hand size — skip
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        all_spectral = SAFE_SPECTRAL_CONSUMABLES | SPECTRAL_TARGETING.keys()
        if not any(c.get("key", "") in all_spectral for c in cards):
            return None  # not a Spectral pack

        jokers = state.get("jokers", {}).get("cards", [])
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)

        best_idx = None
        best_score = 0.0

        for i, card in enumerate(cards):
            key = card.get("key", "")
            score = self.SPECTRAL_SCORES.get(key, 0.0)
            if score == 0.0:
                continue

            # Apply runtime conditions
            if key in ("c_ankh", "c_hex") and not jokers:
                score = 0.0
            elif key == "c_hex":
                # Hex destroys ALL jokers except 1 — run-ending in late game.
                # Block if: joker slots full, any scaling joker active, or ante >= 5.
                joker_count = joker_slots.get("count", 0)
                joker_limit = joker_slots.get("limit", 5)
                owned_keys = {j.get("key") for j in jokers}
                if joker_count >= joker_limit:
                    score = 0.0  # all slots filled — established lineup
                elif owned_keys & SCALING_JOKERS:
                    score = 0.0  # would destroy scaling investment
                elif ante >= 5:
                    score = 0.0  # too late to rebuild
            elif key == "c_ankh":
                # Ankh clones a joker — needs a free slot for the copy
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    score = 0.0
            elif key == "c_wraith":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    score = 0.0
            elif key == "c_ectoplasm":
                if not jokers or ante < 3:
                    score = 0.0

            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx is None:
            return PackAction(card_index=None, reason="skip spectral pack (nothing useful)")

        key = cards[best_idx].get("key", "")
        label = cards[best_idx].get("label", "?")

        # Targeting spectrals need a target card from the current hand
        if key in SPECTRAL_TARGETING:
            hand_cards = state.get("hand", {}).get("cards", [])
            strat = compute_strategy(jokers, state.get("hands", {}))
            max_count, effect_type, extra = SPECTRAL_TARGETING[key]
            targets, _ = _find_tarot_targets(effect_type, extra, max_count, hand_cards, jokers, strat)
            if not targets:
                return PackAction(card_index=None, reason=f"skip spectral pack ({label} needs target, none available)")
            return PackAction(card_index=best_idx, targets=targets, reason=f"spectral pick: {label} (score={best_score:.1f}) -> targets {targets}")

        return PackAction(card_index=best_idx, reason=f"spectral pick: {label} (score={best_score:.1f})")


class PickBestFromPack:
    """Fallback: pick the first non-targeting card, or skip."""
    name = "pick_best_from_pack"

    # Tarot cards that require target card selection — we can't use these
    # from packs since we don't have target selection logic yet.
    NEEDS_TARGETS = {
        "c_magician", "c_high_priestess", "c_empress", "c_emperor",
        "c_hierophant", "c_lovers", "c_chariot", "c_justice", "c_hermit",
        "c_wheel_of_fortune", "c_strength", "c_hanged_man", "c_death",
        "c_temperance", "c_devil", "c_tower", "c_star", "c_moon", "c_sun",
        "c_judgement", "c_world",
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return PackAction(card_index=None, reason="skip empty pack")

        # Pick first card that doesn't need targets
        for i, card in enumerate(cards):
            key = card.get("key", "")
            if key not in self.NEEDS_TARGETS:
                return PackAction(card_index=i, reason=f"pick: {card.get('label', '?')}")

        # All cards need targets — skip the pack
        return PackAction(card_index=None, reason="skip pack (all cards need targets)")


# ---------------------------------------------------------------------------
# Rules: Use consumables
# ---------------------------------------------------------------------------

# Planet card keys and what they level
PLANET_KEYS: dict[str, str] = {
    "c_mercury": "Pair",
    "c_venus": "Three of a Kind",
    "c_earth": "Full House",
    "c_mars": "Four of a Kind",
    "c_jupiter": "Flush",
    "c_saturn": "Straight",
    "c_uranus": "Two Pair",
    "c_neptune": "Straight Flush",
    "c_pluto": "High Card",
    "c_planet_x": "Five of a Kind",
    "c_ceres": "Flush House",
    "c_eris": "Flush Five",
    "c_black_hole": "ALL",  # levels every hand type
}

# No-target Tarots safe to use from consumable slot
SAFE_CONSUMABLE_TAROTS = {
    "c_judgement",         # creates random Joker
    "c_high_priestess",   # creates 2 random Planets
    "c_hermit",           # doubles money (max $20)
    "c_emperor",          # creates 2 random Tarots
    "c_temperance",       # money = joker sell values
    "c_wheel_of_fortune", # 1/4 edition on random joker
    "c_fool",             # copies last used Tarot/Planet — fire immediately
}


# Targeting Tarots that are permanent deck changes — use immediately, not tactically
IMMEDIATE_TARGETING = {"destroy", "clone", "stone", "rank_up", "deck_enhance", "seal", "edition", "clone_deck"}
# Targeting Tarots that benefit from tactical timing — use based on current hand
TACTICAL_TARGETING = {"suit_convert", "glass", "enhance", "gold"}

# No-target Spectral cards with conditional use logic
SAFE_SPECTRAL_CONSUMABLES = {
    "c_ankh",       # clone a random joker — only if jokers exist
    "c_immolate",   # destroy 5 cards, gain $20 — deck thin + money
    "c_ectoplasm",  # ×1 xmult on random joker, -1 hand size — skip if ante < 3
    "c_hex",        # polychrome on random joker, destroys other consumables — only if jokers exist
    "c_wraith",     # create random Rare joker, sets money $0 — only if joker slot open
    # c_sigil: random suit conversion — too unpredictable, intentionally excluded
    # c_ouija: permanent -1 hand size — almost never worth it, intentionally excluded
}

# Targeting Spectral cards — permanent deck improvements, fire immediately
SPECTRAL_TARGETING: dict[str, tuple] = {
    # Destroy 1 junk card, add enhanced cards to deck
    "c_familiar":    (1, "deck_enhance", "face"),    # destroy 1, add 3 enhanced face cards
    "c_grim":        (1, "deck_enhance", "ace"),     # destroy 1, add 2 enhanced aces
    "c_incantation": (1, "deck_enhance", "number"),  # destroy 1, add 4 enhanced numbers
    # Seal application
    "c_talisman":    (1, "seal", "Gold"),    # $3 when played
    "c_deja_vu":     (1, "seal", "Red"),     # replay the card
    "c_trance":      (1, "seal", "Blue"),    # planet card when held at end of round
    "c_medium":      (1, "seal", "Purple"),  # tarot when discarded
    # Edition
    "c_aura":        (1, "edition", None),   # Foil/Holo/Poly on a card
    # Deck duplication
    "c_cryptid":     (1, "clone_deck", None),
}


class UseImmediateConsumables:
    """Use planets, no-target tarots, and permanent deck-change tarots immediately."""
    name = "use_immediate_consumables"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)

        # Priority 1: Use planet cards immediately
        strat = compute_strategy(jokers, hand_levels)
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key in PLANET_KEYS:
                hand_type = PLANET_KEYS[key]
                affinity = strat.hand_affinity(hand_type) if hand_type != "ALL" else 99
                return UseConsumable(
                    i, reason=f"use planet: {card.get('label', '?')} (levels {hand_type}, affinity={affinity:.0f})",
                )

        # Priority 2: Use safe no-target Tarots
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key in SAFE_CONSUMABLE_TAROTS:
                if key == "c_judgement":
                    if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                        continue
                return UseConsumable(
                    i, reason=f"use tarot: {card.get('label', '?')}",
                )

        # Priority 2.5: Use safe no-target Spectral cards (with per-card conditions)
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key not in SAFE_SPECTRAL_CONSUMABLES:
                continue
            if key in ("c_ankh", "c_hex") and not jokers:
                continue  # nothing to clone/buff
            if key == "c_hex":
                # Hex destroys ALL jokers except 1 — sell it when established
                joker_count = joker_slots.get("count", 0)
                joker_limit = joker_slots.get("limit", 5)
                owned_keys = {j.get("key") for j in jokers}
                if joker_count >= joker_limit or owned_keys & SCALING_JOKERS or ante >= 5:
                    reason = "full slots" if joker_count >= joker_limit else (
                        "scaling joker" if owned_keys & SCALING_JOKERS else f"ante {ante}"
                    )
                    return SellConsumable(i, reason=f"sell Hex (would destroy joker lineup: {reason})")

            if key == "c_ankh":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    continue  # no slot for the cloned joker
            if key == "c_wraith":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    continue  # no slot for the rare joker
            if key == "c_ectoplasm" and ante < 3:
                continue  # -1 hand size too costly early game
            return UseConsumable(
                i, reason=f"use spectral: {card.get('label', '?')}",
            )

        # Priority 3: Use permanent targeting Tarots and Spectrals
        if hand_cards:
            strat = compute_strategy(jokers, hand_levels)
            money = state.get("money", 0)
            rnd = state.get("round", {})
            discards_left = rnd.get("discards_left", 0)
            hands_left = rnd.get("hands_left", 1)
            joker_limit = state.get("jokers", {}).get("limit", 5)
            current_best = best_hand(hand_cards, hand_levels, jokers=jokers,
                                     money=money, discards_left=discards_left, hands_left=hands_left,
                                     joker_limit=joker_limit)

            # Check Tarots first
            for i, card in enumerate(consumables):
                key = card.get("key", "")
                if key not in TARGETING_TAROTS:
                    continue
                max_count, effect_type, extra = TARGETING_TAROTS[key]
                if effect_type not in IMMEDIATE_TARGETING:
                    continue
                targets, score = _find_tarot_targets(
                    effect_type, extra, max_count, hand_cards, jokers, strat,
                    current_best=current_best,
                )
                if targets:
                    return UseConsumable(
                        i, target_cards=targets,
                        reason=f"use tarot: {card.get('label', '?')} -> targets {targets}",
                    )

            # Check Spectral targeting cards
            for i, card in enumerate(consumables):
                key = card.get("key", "")
                if key not in SPECTRAL_TARGETING:
                    continue
                max_count, effect_type, extra = SPECTRAL_TARGETING[key]
                targets, score = _find_tarot_targets(
                    effect_type, extra, max_count, hand_cards, jokers, strat,
                    current_best=current_best,
                )
                if targets:
                    return UseConsumable(
                        i, target_cards=targets,
                        reason=f"use spectral: {card.get('label', '?')} -> targets {targets}",
                    )

        # Priority 4: Sell unknown consumables to free slots
        all_known = PLANET_KEYS.keys() | SAFE_CONSUMABLE_TAROTS | TARGETING_TAROTS.keys() | SAFE_SPECTRAL_CONSUMABLES | SPECTRAL_TARGETING.keys()
        consumable_limit = state.get("consumables", {}).get("limit", 2)
        if len(consumables) >= consumable_limit:
            for i, card in enumerate(consumables):
                key = card.get("key", "")
                if key not in all_known:
                    return SellConsumable(
                        i, reason=f"sell unknown consumable: {card.get('label', '?')} (freeing slot)",
                    )

        return None


class UseTacticalConsumables:
    """Use suit conversions, Glass, and enhancements tactically based on current hand.

    Evaluates whether using a Tarot would create a significantly better hand
    than what we currently have (e.g., converting off-suit cards to create a Flush).
    """
    name = "use_tactical_consumables"

    # Minimum score improvement to justify using a consumable
    MIN_IMPROVEMENT = 1.3  # new hand must be 30% better

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        if not hand_cards:
            return None

        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})

        # Find tactical consumables
        tactical = []
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key in TARGETING_TAROTS:
                max_count, effect_type, extra = TARGETING_TAROTS[key]
                if effect_type in TACTICAL_TARGETING:
                    tactical.append((i, key, max_count, effect_type, extra))

        if not tactical:
            return None

        strat = compute_strategy(jokers, hand_levels)
        money = state.get("money", 0)
        rnd = state.get("round", {})
        discards_left = rnd.get("discards_left", 0)
        hands_left = rnd.get("hands_left", 1)

        # Blind progress — how many chips still needed
        blind_score = 0
        chips_scored = rnd.get("chips", 0)
        for b in state.get("blinds", {}).values():
            if isinstance(b, dict) and b.get("status") == "CURRENT":
                blind_score = b.get("score", 0)
                break
        chips_remaining = max(0, blind_score - chips_scored)

        # Desperation: last 2 hands and current score won't clear the blind
        desperate = hands_left <= 2

        joker_limit = state.get("jokers", {}).get("limit", 5)
        current_best = best_hand(hand_cards, hand_levels, jokers=jokers,
                                 money=money, discards_left=discards_left, hands_left=hands_left,
                                 joker_limit=joker_limit)
        current_score = current_best.total if current_best else 0

        # If we can already beat the blind with chips in hand, don't burn consumables
        # unless we're on the last hand (use it or lose it)
        can_win_now = current_score >= chips_remaining

        # Gold enhancement: fire NOW when we know we're winning — the target card
        # will sit in hand collecting $3/round. Only when the round is already won.
        if can_win_now:
            for cons_idx, key, max_count, effect_type, extra in tactical:
                if effect_type != "gold":
                    continue
                targets = _find_gold_targets(hand_cards, max_count, current_best)
                if targets:
                    label = consumables[cons_idx].get("label", "?")
                    return UseConsumable(
                        cons_idx, target_cards=targets,
                        reason=f"gold junk: {label} on held card (round already won)",
                    )

        if can_win_now and hands_left > 1:
            return None

        best_action = None
        best_new_score = current_score

        for cons_idx, key, max_count, effect_type, extra in tactical:
            label = consumables[cons_idx].get("label", "?")

            if effect_type == "suit_convert":
                # Suit conversions are time-sensitive: the hand setup (4 of a suit)
                # may not recur. Use whenever it creates a Flush improvement.
                result = self._eval_suit_convert(
                    hand_cards, hand_levels, jokers, extra, max_count,
                    current_score, strat, money, discards_left, hands_left,
                    joker_limit=joker_limit,
                )
                if result and result[0] > best_new_score:
                    best_new_score, targets = result
                    best_action = UseConsumable(
                        cons_idx, target_cards=targets,
                        reason=f"tactical: {label} -> Flush ({best_new_score} vs {current_score}, {hands_left}h left)",
                    )

            elif effect_type in ("glass", "enhance"):
                # Permanent enhancements: save until desperate or last hand.
                # They improve the deck long-term, so don't burn early.
                if not desperate:
                    continue

                if effect_type == "glass":
                    result = self._eval_glass(
                        hand_cards, hand_levels, jokers, current_best, current_score,
                    )
                    if result and result[0] > best_new_score:
                        best_new_score, targets = result
                        best_action = UseConsumable(
                            cons_idx, target_cards=targets,
                            reason=f"desperate: Glass on face card ({best_new_score} vs {current_score}, {hands_left}h left)",
                        )
                else:
                    enhancement = extra
                    result = self._eval_enhancement(
                        hand_cards, hand_levels, jokers, current_best, current_score,
                        enhancement, max_count,
                    )
                    if result and result[0] > best_new_score:
                        best_new_score, targets = result
                        best_action = UseConsumable(
                            cons_idx, target_cards=targets,
                            reason=f"desperate: {label} ({enhancement}) ({best_new_score} vs {current_score}, {hands_left}h left)",
                        )

        # Suit conversions still need to clear the improvement bar.
        # Desperate enhancements just need to be better than current.
        if best_action and best_new_score > current_score * self.MIN_IMPROVEMENT:
            return best_action

        return None

    def _eval_suit_convert(
        self, hand_cards, hand_levels, jokers, target_suit, max_count,
        current_score, strat, money=0, discards_left=0, hands_left=1,
        joker_limit: int = 5,
    ) -> tuple[int, list[int]] | None:
        """Would converting cards to target_suit create a Flush?"""
        from hand_evaluator import card_suit, card_suits, score_hand

        # Count how many cards already match the target suit
        matching = []
        non_matching = []
        for i, c in enumerate(hand_cards):
            suits = card_suits(c)
            if target_suit in suits:
                matching.append(i)
            elif card_suit(c) is not None:
                non_matching.append(i)

        # Need at least 3 matching + enough convertible to reach 5
        needed = 5 - len(matching)
        if needed <= 0:
            return None  # already have a flush
        if needed > min(max_count, len(non_matching)):
            return None  # can't convert enough

        # Pick the lowest-value non-matching cards to convert
        from hand_evaluator import card_rank, rank_value
        non_matching.sort(key=lambda i: rank_value(card_rank(hand_cards[i]) or "2"))
        targets = non_matching[:needed]

        # Simulate: what would the Flush score?
        flush_cards = matching[:5 - needed]
        flush_cards.extend(targets)
        flush_cards = flush_cards[:5]
        simulated = [hand_cards[i] for i in flush_cards]
        held = [hand_cards[i] for i in range(len(hand_cards)) if i not in set(flush_cards)]
        _, _, flush_score = score_hand(
            "Flush", simulated, hand_levels,
            jokers=jokers, played_cards=simulated, held_cards=held,
            money=money, discards_left=discards_left, hands_left=hands_left,
            joker_limit=joker_limit,
        )

        return (flush_score, targets)

    def _eval_glass(
        self, hand_cards, hand_levels, jokers, current_best, current_score,
    ) -> tuple[int, list[int]] | None:
        """Would applying Glass to a face card in our best hand boost scoring?"""
        if not current_best:
            return None
        from hand_evaluator import card_rank, _modifier

        # Find face cards in the best hand's scoring cards that aren't enhanced
        for idx in current_best.card_indices:
            c = hand_cards[idx]
            r = card_rank(c)
            if r in FACE_RANKS_TAROT and not _modifier(c).get("enhancement"):
                # Glass gives ×2 mult when scored — rough estimate: double the score
                estimated_new = int(current_score * 1.8)  # conservative estimate
                return (estimated_new, [idx])

        return None

    def _eval_enhancement(
        self, hand_cards, hand_levels, jokers, current_best, current_score,
        enhancement, max_count,
    ) -> tuple[int, list[int]] | None:
        """Would applying this enhancement boost cards about to score?"""
        if not current_best:
            return None
        from hand_evaluator import card_rank, card_suit, card_suits, _modifier

        # Wild (Lovers) is special — check if it would enable a Flush
        if enhancement == "Wild":
            # A Wild card counts as ALL suits. Check if we're 1 card away from Flush
            from hand_evaluator import flush_draw
            fd = flush_draw(hand_cards)
            if fd and len(fd) >= 4:
                # Find a non-matching card in hand that isn't already Wild
                for i, c in enumerate(hand_cards):
                    if i not in fd and not _modifier(c).get("enhancement"):
                        # Making this Wild would complete the flush possibility
                        estimated = int(current_score * 1.5)
                        return (estimated, [i])

        # Steel — value comes from cards held in hand (NOT played). Apply to highest-rank
        # unenhanced card that will stay in hand this round, so it contributes ×1.5 mult NOW.
        if enhancement == "Steel":
            scoring_set = set(current_best.card_indices)
            held = [i for i, c in enumerate(hand_cards)
                    if i not in scoring_set and not _modifier(c).get("enhancement")]
            if held:
                from hand_evaluator import card_rank
                RANK_ORDER = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"]
                held.sort(key=lambda i: RANK_ORDER.index(card_rank(hand_cards[i]))
                          if card_rank(hand_cards[i]) in RANK_ORDER else -1, reverse=True)
                estimated = int(current_score * 1.5)  # ×1.5 mult per Steel held
                return (estimated, held[:max_count])
            return None

        # For other enhancements, apply to highest-value non-enhanced scoring card
        targets = _find_enhancement_targets(hand_cards, max_count)
        if targets:
            # Check if target is in the current best hand (about to be played)
            scoring_set = set(current_best.card_indices)
            relevant = [t for t in targets if t in scoring_set]
            if relevant:
                estimated = int(current_score * 1.2)  # modest boost
                return (estimated, relevant[:max_count])

        return None


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

# Default rule sets by game state
DEFAULT_RULES: dict[str, list[Rule]] = {
    "SELECTING_HAND": [
        VerdantLeafUnlock(),
        UseImmediateConsumables(),
        MilkScalingJokers(),
        ReorderJokersForCeremonial(),
        UseTacticalConsumables(),
        PlayWinningHand(),
        DiscardToImprove(),
        PlayHighValueHand(),
        PlayBestAvailable(),
    ],
    "BLIND_SELECT": [
        SkipForTag(),
        AlwaysSelectBlind(),
    ],
    "SHOP": [
        SellWeakJoker(),
        FeedCampfire(),
        ReorderJokersForCeremonial(),
        BuyJokersInShop(),
        BuyConsumablesInShop(),
        BuyPacksInShop(),
        BuyVouchersInShop(),
        RerollShop(),
        LeaveShop(),
    ],
    "ROUND_EVAL": [
        AlwaysCashOut(),
    ],
    "TAROT_PACK": [SkipPackForRedCard(), PickFromTarotPack(), PickBestFromPack()],
    "PLANET_PACK": [PickFromPlanetPack(), PickBestFromPack()],  # never skip planets
    "SPECTRAL_PACK": [SkipPackForRedCard(), PickBestFromPack()],
    "STANDARD_PACK": [SkipPackForRedCard(), PickBestFromPack()],
    "BUFFOON_PACK": [PickFromBuffoonPack(), PickBestFromPack()],  # never skip free jokers
    "SMODS_BOOSTER_OPENED": [SkipPackForRedCard(), PickFromTarotPack(), PickFromPlanetPack(), PickFromBuffoonPack(), PickFromSpectralPack(), PickBestFromPack()],
}


class RuleEngine:
    """
    Evaluates rules in priority order for the current game state.

    Usage:
        engine = RuleEngine()
        action = engine.decide(game_state)
        method, params = action.to_rpc()
    """

    def __init__(self, rules: dict[str, list[Rule]] | None = None) -> None:
        self.rules = rules or dict(DEFAULT_RULES)

    def add_rule(self, game_state: str, rule: Rule, priority: int | None = None) -> None:
        """Insert a rule at a given priority (0 = highest). Appends if None."""
        if game_state not in self.rules:
            self.rules[game_state] = []
        if priority is None:
            self.rules[game_state].append(rule)
        else:
            self.rules[game_state].insert(priority, rule)

    def decide(self, state: dict[str, Any]) -> Action | None:
        """
        Run rules for the current game state. Returns the first Action that
        fires, or None if no rule matched.
        """
        state.pop("_round_ctx", None)

        game_state = state.get("state", "")
        rules = self.rules.get(game_state, [])

        try:
            for rule in rules:
                action = rule.evaluate(state)
                if action is not None:
                    return action
        finally:
            state.pop("_round_ctx", None)

        return None
