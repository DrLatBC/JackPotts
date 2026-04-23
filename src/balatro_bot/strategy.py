"""
Strategy module — derives a coherent game plan from owned jokers and hand levels.

The strategy tells the bot what hand types and suits to favor across all
decisions: hand play, discard targets, joker purchases, planet picks.

Recomputed whenever jokers change (after shop, after pack pick).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key

if TYPE_CHECKING:
    from typing import Any
    from balatro_bot.domain.models.deck_profile import DeckProfile


# ---------------------------------------------------------------------------
# Build archetypes — cross-cutting strategies beyond poker hand types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchetypeProfile:
    """Defines a build archetype that cuts across poker hand types.

    Archetypes inject weights into existing hand/rank/suit affinity channels,
    so 35+ existing decision points benefit automatically.
    """
    name: str                            # "face_card"
    display_name: str                    # "Face Card"
    joker_weights: dict[str, int]        # member joker key -> weight (xmult=5, mult=3, chips=2, retrigger=4)
    amplifiers: frozenset[str]           # jokers that amplify (e.g., Pareidolia for face card)
    hand_contributions: dict[str, float] # hand_type -> weight per strength point
    rank_contributions: dict[str, float] # rank -> weight per strength point
    suit_contributions: dict[str, float] # suit -> weight per strength point
    anti_jokers: frozenset[str]          # jokers that conflict with this archetype


# Pareidolia makes ALL cards face cards — hands with more scoring cards win
_PAREIDOLIA_FACE_CONTRIBUTIONS: dict[str, float] = {
    "Straight Flush": 6.0,
    "Flush": 5.0,
    "Straight": 5.0,
    "Full House": 4.0,
    "Four of a Kind": 3.0,
    "Two Pair": 3.0,
    "Three of a Kind": 2.0,
    "Pair": 1.0,
    "High Card": 1.0,
}

# Shared hand contributions for rank-scoring archetypes:
# these hand types put specific ranks into the scoring set
_RANK_SCORING_HANDS: dict[str, float] = {
    "High Card": 1.0,
    "Pair": 3.0,
    "Two Pair": 2.0,
    "Three of a Kind": 3.0,
    "Four of a Kind": 2.0,
    "Full House": 2.0,
}

ARCHETYPE_REGISTRY: dict[str, ArchetypeProfile] = {
    "face_card": ArchetypeProfile(
        name="face_card",
        display_name="Face Card",
        joker_weights={
            "j_photograph": 5,       # xMult on first face scored
            "j_triboulet": 5,        # xMult per K/Q scored
            "j_sock_and_buskin": 4,  # retrigger face cards
            "j_smiley": 3,           # +mult per face scored
            "j_scary_face": 2,       # +chips per face scored
            "j_midas_mask": 1,       # face cards → Gold (economy)
        },
        amplifiers=frozenset({"j_pareidolia"}),
        hand_contributions={
            "High Card": 4.0,       # face cards always in scoring set
            "Pair": 3.0,            # pair of face cards is ideal
            "Full House": 3.0,      # face cards in both parts
            "Two Pair": 2.0,        # face pairs
            "Three of a Kind": 2.0, # trips of face rank
        },
        rank_contributions={"J": 3.0, "Q": 3.0, "K": 3.0},
        suit_contributions={},
        anti_jokers=frozenset({"j_ride_the_bus"}),
    ),
    "single_rank": ArchetypeProfile(
        name="single_rank",
        display_name="Rank Scoring",
        joker_weights={
            "j_even_steven": 3,    # all evens
            "j_odd_todd": 2,       # all odds
            "j_fibonacci": 5,      # retrigger A,2,3,5,8
            "j_hack": 5,           # retrigger 2,3,4,5
            "j_scholar": 3,        # Aces
            "j_walkie_talkie": 3,  # T,4
            "j_8_ball": 2,         # 8s
            "j_sixth_sense": 2,    # 6s
            "j_wee": 2,            # 2s
            # j_superposition intentionally omitted — hard-blacklisted.
            # Multi-conditional trigger (Ace + Straight) for a single
            # tarot reward. Never worth a slot.
        },
        amplifiers=frozenset(),
        hand_contributions=_RANK_SCORING_HANDS,
        rank_contributions={},      # ranks already in JOKER_RANK_AFFINITY
        suit_contributions={},
        anti_jokers=frozenset(),
    ),
    "probability": ArchetypeProfile(
        name="probability",
        display_name="Probability",
        joker_weights={
            "j_lucky_cat":   5,  # scales xMult on Lucky triggers
            "j_bloodstone":  4,  # chance of xMult on Heart scoring
            "j_8_ball":      3,  # chance of tarot on scored 8
            "j_space":       3,  # chance to level hand
            "j_sixth_sense": 3,  # chance of spectral on played 6
            "j_oops":        5,  # doubles all probabilities — amplifier itself counts
        },
        amplifiers=frozenset({"j_oops"}),
        hand_contributions={},
        rank_contributions={"8": 1.0, "6": 1.0},
        suit_contributions={"H": 1.0},
        anti_jokers=frozenset(),
    ),
    "fibonacci": ArchetypeProfile(
        name="fibonacci",
        display_name="Fibonacci",
        joker_weights={
            "j_fibonacci": 5,      # retrigger A,2,3,5,8
            "j_hack": 5,           # retrigger 2,3,4,5 — shares 2,3,5
        },
        amplifiers=frozenset(),
        hand_contributions={
            **_RANK_SCORING_HANDS,
            "Straight": 2.0,       # A-5 straight hits both jokers
        },
        rank_contributions={
            "A": 3.0, "2": 3.0, "3": 3.0, "4": 1.0, "5": 3.0, "8": 3.0,
        },
        suit_contributions={},
        anti_jokers=frozenset(),
    ),
}


# ---------------------------------------------------------------------------
# Affinity tables
# ---------------------------------------------------------------------------

# Joker key -> (hand_types, weight)
# Weight reflects impact: +chips=1, +mult=2, xmult=5
JOKER_HAND_AFFINITY: dict[str, tuple[list[str], int]] = {
    # +mult on hand type (weight 2)
    "j_jolly": (["Pair"], 2),
    "j_zany": (["Three of a Kind"], 2),
    "j_mad": (["Two Pair"], 2),
    "j_crazy": (["Straight"], 2),
    "j_droll": (["Flush"], 2),

    # +chips on hand type (weight 1)
    "j_sly": (["Pair"], 1),
    "j_wily": (["Three of a Kind"], 1),
    "j_clever": (["Two Pair"], 1),
    "j_devious": (["Straight"], 1),
    "j_crafty": (["Flush"], 1),

    # xmult on hand type — weight reflects how often the hand actually fires
    "j_duo": (["Pair"], 5),           # Pair is extremely common
    "j_trio": (["Three of a Kind"], 4),
    "j_family": (["Four of a Kind"], 1),  # almost never hits without a dedicated deck
    "j_order": (["Straight"], 4),
    "j_tribe": (["Flush"], 5),

    # Conditional +mult by game state
    "j_half": (["High Card", "Pair", "Three of a Kind"], 2),
    "j_trousers": (["Two Pair"], 2),

    # Face card jokers — handled by face_card archetype, not hand type affinity
    # j_fibonacci, j_hack — handled by fibonacci archetype (rank-scoring, not Straight)
    # j_blackboard — held card archetype (cares about held suit, not Flush)

    # Scaling jokers with hand type triggers
    "j_runner": (["Straight"], 1),
    "j_square": (["Two Pair", "Four of a Kind"], 1),

    # Special
    "j_seeing_double": (["Flush"], 2),
    "j_flower_pot": (["Flush"], 3),

    # Hand-type enablers — structural probability multipliers. These don't
    # score directly but dramatically raise the hit rate of the hands they
    # enable, so the bot should steer toward them whenever owned.
    "j_shortcut":        (["Straight", "Straight Flush"], 4),  # gap straights (skip-1)
    "j_four_fingers":    (["Flush", "Straight", "Straight Flush",
                           "Flush House", "Flush Five"], 4),   # 4-card flushes/straights
    "j_smeared":         (["Flush", "Straight Flush",
                           "Flush House", "Flush Five"], 3),   # wild suit merging
    "j_splash":          (["Flush", "Straight Flush",
                           "Flush House", "Flush Five"], 2),   # all cards count in flush

    # Hand-type-diversity jokers. Driver's License needs 16+ enhanced cards
    # (hand-type-agnostic trigger) but synergizes with any high-scoring hand
    # that gets replayed. Obelisk scales xmult on non-favorite hands, so it
    # biases toward playing the rarer/higher hands.
    "j_drivers_license": (["Full House", "Straight Flush",
                           "Flush House", "Flush Five"], 1),
    "j_obelisk":         (["Straight", "Flush", "Full House",
                           "Straight Flush", "Flush House", "Flush Five"], 2),
}

# Joker key -> (ranks, weight)
# Weight reflects impact: retrigger=5, +mult=3, +chips=2, utility trigger=2
JOKER_RANK_AFFINITY: dict[str, tuple[list[str], int]] = {
    # Retrigger specific ranks (double scoring on these cards)
    "j_hack":       (["2", "3", "4", "5"], 5),
    "j_fibonacci":  (["A", "2", "3", "5", "8"], 5),

    # +mult on rank group
    "j_even_steven": (["2", "4", "6", "8", "T"], 3),
    "j_odd_todd":    (["A", "3", "5", "7", "9"], 2),  # +chips, lower weight

    # Utility triggers on specific ranks
    "j_8_ball":      (["8"], 2),          # score an 8 → tarot
    "j_sixth_sense": (["6"], 2),          # play a 6 → spectral
    "j_wee":         (["2"], 2),          # +chips when 2 scored, scaling

    # Per-card scoring on specific ranks
    "j_walkie_talkie": (["T", "4"], 3),   # +chips and +mult per T or 4
    "j_scholar":       (["A"], 3),         # +chips and +mult per Ace
    "j_triboulet":     (["K", "Q"], 5),    # xMult per K or Q scored
    "j_baron":         (["K"], 4),         # xMult per held King
    "j_shoot_the_moon": (["Q"], 3),        # +mult per held Queen
    # Anti-affinity for face cards (negative weight)
    "j_ride_the_bus": (["J", "Q", "K"], -3),  # resets mult on face cards

    # j_superposition intentionally omitted — hard-blacklisted in
    # shop_valuation. Despite targeting Aces, its effect (spawn a tarot
    # when the hand is a Straight *and* contains an Ace) is too
    # conditional to justify roster influence.
}

# Joker key -> (suit, weight)
JOKER_SUIT_AFFINITY: dict[str, tuple[str, int]] = {
    "j_greedy_joker": ("D", 2),
    "j_lusty_joker": ("H", 2),
    "j_wrathful_joker": ("S", 2),
    "j_gluttenous_joker": ("C", 2),
    "j_arrowhead": ("S", 1),
    "j_onyx_agate": ("C", 3),
    "j_bloodstone": ("H", 3),
    "j_rough_gem": ("D", 1),
}

# Joker key -> (enhancements, weight). Jokers that care about specific
# card enhancements — used for card protection during discards.
JOKER_ENHANCEMENT_AFFINITY: dict[str, tuple[list[str], int]] = {
    "j_steel_joker":  (["STEEL"], 5),
    "j_stone":        (["STONE"], 5),
    "j_lucky_cat":    (["LUCKY"], 4),
    "j_glass":        (["GLASS"], 3),
    "j_golden":       (["GOLD"], 2),
    # Vampire eats enhancements from scoring cards for permanent xmult gain.
    # Anti-affinity for the enhancements it can consume — we want to build
    # toward enhancements, not protect them, when Vampire is the primary
    # scaler. Weight chosen to offset (not fully cancel) a single positive
    # affinity source, since Vampire pairs fine with Steel/Glass holders.
    "j_vampire":      (["STEEL", "GLASS", "GOLD", "LUCKY", "STONE",
                        "BONUS", "MULT", "WILD"], -3),
}


# ---------------------------------------------------------------------------
# CardProtection — unified discard-priority scoring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CardProtection:
    """Protection score per card attribute. Higher = more valuable to keep.

    Consolidates all signals that should shield a card from discard:
    boss blind suit constraints, rank affinity from jokers, suit affinity,
    enhancement affinity, and the current round's Idol target card.

    `score(card)` returns a float per card — callers sort ascending (lowest
    protection first = discard first).
    """
    rank_affinity: dict[str, float] = field(default_factory=dict)
    suit_affinity: dict[str, float] = field(default_factory=dict)
    enhancement_affinity: dict[str, float] = field(default_factory=dict)
    idol_rank: str | None = None
    idol_suit: str | None = None
    scoring_suit: str | None = None         # boss-blind forced suit (Head/Club/Window)
    debuffed_suit: str | None = None        # boss-blind debuffed suit (The Goad = Spades)
    blackboard: bool = False                # require Spades/Clubs in played hand
    discards_left: int = 0                  # affects purple-seal anti-protection
    heavy_debuff: bool = False              # Pillar: debuffed = permanently dead this round

    def score(self, card) -> float:
        """Protection score for a single card. Higher = keep."""
        from balatro_bot.cards import card_rank, card_suit, card_suits, is_debuffed, rank_value, _modifier

        total = 0.0
        rank = card_rank(card)
        suit = card_suit(card)
        all_suits = card_suits(card)
        mod = _modifier(card)
        enhancement = mod.get("enhancement") if isinstance(mod, dict) else None
        seal = mod.get("seal") if isinstance(mod, dict) else None

        # Hard constraints: boss blind / Blackboard — these are binary protections
        if self.scoring_suit and self.scoring_suit in all_suits:
            total += 50.0
        if self.debuffed_suit and self.debuffed_suit in all_suits:
            total -= 50.0
        if self.blackboard and suit in ("S", "C"):
            total += 40.0

        # Idol target — specific rank+suit match
        if self.idol_rank and self.idol_suit and rank == self.idol_rank and self.idol_suit in all_suits:
            total += 20.0

        # Strategy affinities
        if rank and self.rank_affinity:
            total += self.rank_affinity.get(rank, 0.0)
        if self.suit_affinity:
            for s in all_suits:
                total += self.suit_affinity.get(s, 0.0)
        if enhancement and self.enhancement_affinity:
            total += self.enhancement_affinity.get(enhancement, 0.0)

        # Seal value — Red/Gold/Blue protect, Purple anti-protects (prefer discard).
        # Purple only anti-protects when we actually have discards available.
        if seal == "RED":
            total += 15.0
        elif seal == "BLUE":
            total += 8.0
        elif seal == "GOLD":
            total += 6.0
        elif seal == "PURPLE" and self.discards_left > 0:
            total -= 10.0

        # Debuff penalty. Normally small — debuffed cards may still be useful
        # for hand classification (e.g. a debuffed face in a Pair still counts).
        # Under The Pillar, the debuff is permanent for the round and the card
        # is pure dead weight — push strongly toward discard.
        if is_debuffed(card):
            total -= 20.0 if self.heavy_debuff else 0.5

        # Raw card value tiebreaker — higher ranks slightly more valuable
        if rank:
            total += rank_value(rank) * 0.01

        return total


# ---------------------------------------------------------------------------
# Strategy dataclass
# ---------------------------------------------------------------------------

@dataclass
class Strategy:
    """The bot's current strategic focus, derived from owned jokers."""

    preferred_hands: list[tuple[str, float]]
    preferred_suits: list[tuple[str, float]]
    preferred_ranks: list[tuple[str, float]] = field(default_factory=list)
    preferred_enhancements: list[tuple[str, float]] = field(default_factory=list)
    active_archetypes: list[tuple[str, float]] = field(default_factory=list)

    def top_hand(self) -> str | None:
        return self.preferred_hands[0][0] if self.preferred_hands else None

    def hand_affinity(self, hand_name: str) -> float:
        for name, score in self.preferred_hands:
            if name == hand_name:
                return score
        return 0.0

    def top_suit(self) -> str | None:
        return self.preferred_suits[0][0] if self.preferred_suits else None

    def suit_affinity(self, suit: str) -> float:
        for s, score in self.preferred_suits:
            if s == suit:
                return score
        return 0.0

    def rank_affinity(self, rank: str) -> float:
        for r, score in self.preferred_ranks:
            if r == rank:
                return score
        return 0.0

    def rank_affinity_dict(self) -> dict[str, float]:
        """Return rank affinity as a dict for fast lookup in hot paths."""
        return dict(self.preferred_ranks) if self.preferred_ranks else {}

    def enhancement_affinity(self, enhancement: str | None) -> float:
        """Weighted preference for a specific enhancement (e.g. STEEL, GLASS).

        Positive when the roster has jokers that reward that enhancement
        (Steel Joker, Lucky Cat, etc.); negative when anti-affinity jokers
        (Vampire) want to consume it. Returns 0.0 for unrecognized keys.
        """
        if not enhancement or not self.preferred_enhancements:
            return 0.0
        e_upper = enhancement.upper()
        for e, score in self.preferred_enhancements:
            if e == e_upper:
                return score
        return 0.0

    def card_protection(
        self,
        jokers: list[dict] | None = None,
        idol_rank: str | None = None,
        idol_suit: str | None = None,
        scoring_suit: str | None = None,
        debuffed_suit: str | None = None,
        discards_left: int = 0,
        heavy_debuff: bool = False,
    ) -> CardProtection:
        """Build a CardProtection view from this strategy + round context.

        Jokers contribute enhancement affinity on top of the already-computed
        hand/suit/rank affinities. Round-specific fields (idol target, boss
        scoring suit, Blackboard) are passed in directly.
        """
        enhancement_affinity: dict[str, float] = {}
        blackboard = False
        if jokers:
            for j in jokers:
                key = joker_key(j)
                if key == "j_blackboard":
                    blackboard = True
                if key in JOKER_ENHANCEMENT_AFFINITY:
                    enhs, weight = JOKER_ENHANCEMENT_AFFINITY[key]
                    for e in enhs:
                        enhancement_affinity[e] = enhancement_affinity.get(e, 0.0) + weight

        return CardProtection(
            rank_affinity=self.rank_affinity_dict(),
            suit_affinity=dict(self.preferred_suits),
            enhancement_affinity=enhancement_affinity,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
            scoring_suit=scoring_suit,
            debuffed_suit=debuffed_suit,
            blackboard=blackboard,
            discards_left=discards_left,
            heavy_debuff=heavy_debuff,
        )

    def has_archetype(self, name: str) -> bool:
        return any(n == name for n, _ in self.active_archetypes)

    def archetype_strength(self, name: str) -> float:
        for n, s in self.active_archetypes:
            if n == name:
                return s
        return 0.0

    def describes(self) -> str:
        parts = []
        if self.preferred_hands:
            top3 = [f"{name}({score:.0f})" for name, score in self.preferred_hands[:3]]
            parts.append("hands=" + ",".join(top3))
        if self.preferred_suits:
            top2 = [f"{s}({score:.0f})" for s, score in self.preferred_suits[:2]]
            parts.append("suits=" + ",".join(top2))
        if self.preferred_ranks:
            top3 = [f"{r}({score:.0f})" for r, score in self.preferred_ranks[:3]]
            parts.append("ranks=" + ",".join(top3))
        if self.active_archetypes:
            archs = ", ".join(f"{n}({s:.0f})" for n, s in self.active_archetypes)
            parts.append(f"arch={archs}")
        return " | ".join(parts) if parts else "no preference"


# ---------------------------------------------------------------------------
# Compute strategy from game state
# ---------------------------------------------------------------------------

# Deck signal tuning. The baseline vanilla deck has 25% of each suit (13/52)
# and 4 copies of each rank. Anything above baseline is "signal" — the deck
# has been shaped toward a build. Weights are calibrated so a clearly-built
# deck (e.g. 50% flush-suit or 8 copies of a rank) produces affinity scores
# comparable to an owned +mult joker (weight ~2-3).
_DECK_SUIT_BASELINE = 0.25
_DECK_SUIT_WEIGHT = 12.0     # excess_frac * this → suit affinity score
_DECK_FLUSH_BASELINE = 0.30  # top-suit share beyond this adds Flush affinity
_DECK_FLUSH_WEIGHT = 10.0
_DECK_RANK_BASELINE = 4      # copies per rank in a vanilla deck
_DECK_RANK_PER_EXCESS = 0.5  # per copy above baseline, capped
_DECK_RANK_CAP = 4.0
_DECK_ENH_PER_CARD = 0.5
_DECK_ENH_CAP = 4.0

# Hand-level base contribution. Each planet used is a sunk commitment —
# leveling a hand from 1→5 should generate meaningful affinity even if no
# joker names that hand. 1.5 per level matches an Uncommon joker's weight.
_LEVEL_BASE_WEIGHT = 1.5


def _deck_signal(
    dp: "DeckProfile | None",
) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    """Return (hand_scores, suit_scores, rank_scores, enhancement_scores) contributed by
    the deck's composition. Empty when no deck profile is available.

    This is the "what is my deck physically good at" signal — more reliable than
    joker-implied affinity late game, because the deck grows monotonically while
    shop offers anti-correlate with what you own.
    """
    hand: dict[str, float] = {}
    suit: dict[str, float] = {}
    rank: dict[str, float] = {}
    enh: dict[str, float] = {}
    if dp is None or not getattr(dp, "total_cards", 0):
        return hand, suit, rank, enh
    total = dp.total_cards

    # Suit concentration — each suit above baseline gets proportional affinity
    if dp.suit_counts:
        for s, c in dp.suit_counts.items():
            frac = c / total
            if frac > _DECK_SUIT_BASELINE:
                suit[s] = (frac - _DECK_SUIT_BASELINE) * _DECK_SUIT_WEIGHT
        top_frac = max(dp.suit_counts.values()) / total
        if top_frac > _DECK_FLUSH_BASELINE:
            w = (top_frac - _DECK_FLUSH_BASELINE) * _DECK_FLUSH_WEIGHT
            hand["Flush"] = hand.get("Flush", 0.0) + w
            hand["Straight Flush"] = hand.get("Straight Flush", 0.0) + w * 0.5
            hand["Flush House"] = hand.get("Flush House", 0.0) + w * 0.5
            hand["Flush Five"] = hand.get("Flush Five", 0.0) + w * 0.4

    # Rank density — high-count ranks bias toward that rank AND toward the
    # hand types that exploit them. A deck with 8 copies of a rank genuinely
    # should lean toward Pair / 3oK / 4oK / 5oK of that rank — that's the
    # build the deck affords.
    if dp.rank_counts:
        for r, c in dp.rank_counts.items():
            if c > _DECK_RANK_BASELINE:
                excess = c - _DECK_RANK_BASELINE
                rank[r] = min(_DECK_RANK_CAP, excess * _DECK_RANK_PER_EXCESS)
                hand["Pair"] = hand.get("Pair", 0.0) + excess * 0.1
                hand["Three of a Kind"] = hand.get("Three of a Kind", 0.0) + excess * 0.08
                if c >= 6:
                    hand["Four of a Kind"] = hand.get("Four of a Kind", 0.0) + (c - 5) * 0.08
                if c >= 8:
                    hand["Five of a Kind"] = hand.get("Five of a Kind", 0.0) + (c - 7) * 0.12

    # Enhancement density — cards already enhanced hint at which enhancement
    # jokers (Steel Joker, Lucky Cat, Glass Joker, Stone Joker) would fire often.
    if getattr(dp, "enhancement_counts", None):
        for e, c in dp.enhancement_counts.items():
            if c >= 2 and e:
                enh[e.upper()] = min(_DECK_ENH_CAP, c * _DECK_ENH_PER_CARD)

    return hand, suit, rank, enh


def _level_signal(
    hand_levels: dict[str, dict] | None,
) -> dict[str, float]:
    """Sunk-commitment signal: each planet used generates hand affinity.

    Previously the hand-level map only scaled existing scores by 1.2^(level-1),
    which meant leveling Flush to 5 without owning any Flush joker added zero
    affinity. Now each level above 1 contributes a base weight regardless of
    joker ownership, so planet usage reflects commitment.
    """
    out: dict[str, float] = {}
    if not hand_levels:
        return out
    for ht, info in hand_levels.items():
        lvl = info.get("level", 1) if isinstance(info, dict) else 1
        if lvl > 1:
            out[ht] = (lvl - 1) * _LEVEL_BASE_WEIGHT
    return out


def compute_strategy(
    jokers: list[dict],
    hand_levels: dict[str, dict] | None = None,
    deck_profile: "DeckProfile | None" = None,
) -> Strategy:
    hand_scores: dict[str, float] = {}
    suit_scores: dict[str, float] = {}
    rank_scores: dict[str, float] = {}
    enhancement_scores: dict[str, float] = {}

    for joker in jokers:
        key = joker_key(joker)

        if key in JOKER_ENHANCEMENT_AFFINITY:
            enhs, weight = JOKER_ENHANCEMENT_AFFINITY[key]
            for e in enhs:
                enhancement_scores[e] = enhancement_scores.get(e, 0.0) + weight

        if key in JOKER_HAND_AFFINITY:
            hand_types, weight = JOKER_HAND_AFFINITY[key]
            for ht in hand_types:
                hand_scores[ht] = hand_scores.get(ht, 0) + weight

        if key in JOKER_SUIT_AFFINITY:
            suit, weight = JOKER_SUIT_AFFINITY[key]
            suit_scores[suit] = suit_scores.get(suit, 0) + weight

        if key in JOKER_RANK_AFFINITY:
            ranks, weight = JOKER_RANK_AFFINITY[key]
            for r in ranks:
                rank_scores[r] = rank_scores.get(r, 0) + weight

    # Detect active archetypes and inject their contributions
    joker_keys = {joker_key(j) for j in jokers}
    active_archs: list[tuple[str, float]] = []
    for arch in ARCHETYPE_REGISTRY.values():
        members = {k for k in arch.joker_weights if k in joker_keys}
        amps = joker_keys & arch.amplifiers
        antis = joker_keys & arch.anti_jokers
        if len(members) < 2 and not (len(members) >= 1 and amps):
            continue
        strength = sum(arch.joker_weights[k] for k in members)
        if amps:
            strength *= 1.0 + 0.5 * len(amps)
        if antis:
            strength *= max(0.1, 1.0 - 0.5 * len(antis))

        # Pareidolia swaps face card hand map (all cards = face → more scoring cards = better)
        contribs = arch.hand_contributions
        if arch.name == "face_card" and "j_pareidolia" in joker_keys:
            contribs = _PAREIDOLIA_FACE_CONTRIBUTIONS

        for ht, w in contribs.items():
            hand_scores[ht] = hand_scores.get(ht, 0) + w * strength
        for r, w in arch.rank_contributions.items():
            rank_scores[r] = rank_scores.get(r, 0) + w * strength
        for s, w in arch.suit_contributions.items():
            suit_scores[s] = suit_scores.get(s, 0) + w * strength
        active_archs.append((arch.name, strength))

    # Synthesize composite hand affinities: jokers that boost sub-hands
    # should also contribute to hands that CONTAIN those sub-hands.
    # Full House = Three of a Kind + Pair
    # Straight Flush = Straight + Flush
    # Flush House = Full House + Flush
    # Flush Five = Five of a Kind + Flush
    #
    # Composites are derived from joker/archetype intent only — deck signal is
    # applied AFTER so deck-driven Pair density doesn't inflate Flush House.
    composites = {
        "Full House":      [("Three of a Kind", 0.5), ("Pair", 0.5)],
        "Straight Flush":  [("Straight", 0.7), ("Flush", 0.7)],
        "Flush House":     [("Full House", 0.5), ("Flush", 0.5), ("Three of a Kind", 0.3), ("Pair", 0.3)],
        "Flush Five":      [("Five of a Kind", 0.5), ("Flush", 0.5)],
    }
    for composite, components in composites.items():
        bonus = sum(hand_scores.get(sub, 0) * weight for sub, weight in components)
        if bonus > 0:
            hand_scores[composite] = hand_scores.get(composite, 0) + bonus

    # Deck signal — what the deck is physically good at. Applied AFTER
    # composites so a 2-heavy deck doesn't inflate Flush House / Full House
    # without the joker intent to match. More reliable than joker-implied
    # affinity late game because the deck grows monotonically while shop
    # offers anti-correlate with owned jokers.
    deck_h, deck_s, deck_r, deck_e = _deck_signal(deck_profile)
    for ht, w in deck_h.items():
        hand_scores[ht] = hand_scores.get(ht, 0) + w
    for s, w in deck_s.items():
        suit_scores[s] = suit_scores.get(s, 0) + w
    for r, w in deck_r.items():
        rank_scores[r] = rank_scores.get(r, 0) + w
    for e, w in deck_e.items():
        enhancement_scores[e] = enhancement_scores.get(e, 0.0) + w

    # Hand-level sunk-commitment signal — planet usage locks in a build.
    for ht, w in _level_signal(hand_levels).items():
        hand_scores[ht] = hand_scores.get(ht, 0) + w

    if hand_levels:
        for ht, score in hand_scores.items():
            level = hand_levels.get(ht, {}).get("level", 1)
            if level > 1:
                hand_scores[ht] = score * (1.2 ** (level - 1))

    preferred_hands = sorted(
        [(ht, score) for ht, score in hand_scores.items() if score > 0],
        key=lambda x: -x[1],
    )
    preferred_suits = sorted(
        [(s, score) for s, score in suit_scores.items() if score > 0],
        key=lambda x: -x[1],
    )
    # Include negative affinity (anti-affinity) — consumers use it to penalize
    preferred_ranks = sorted(
        [(r, score) for r, score in rank_scores.items() if score != 0],
        key=lambda x: -x[1],
    )
    preferred_enhancements = sorted(
        [(e, score) for e, score in enhancement_scores.items() if score != 0],
        key=lambda x: -x[1],
    )

    return Strategy(
        preferred_hands=preferred_hands,
        preferred_suits=preferred_suits,
        preferred_ranks=preferred_ranks,
        preferred_enhancements=preferred_enhancements,
        active_archetypes=active_archs,
    )
