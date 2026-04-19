"""ROI-based valuation for economic/utility jokers.

Converts a joker's expected net dollars over the rest of the run into the
same 0-15 scale used by ``shop_valuation.evaluate_joker_value``. Reference
implementations for three primitives:

  - Flat per-round income       (Golden Joker)
  - Conditional per-hand trigger (Business Card)
  - Deck-state-scaled income    (Cloud 9)

Add more valuators by writing a function returning expected dollars and
registering it in ``UTILITY_ROI_VALUATORS``. The ``evaluate`` dispatcher
handles the $ → value-unit conversion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from balatro_bot.domain.models.deck_profile import DeckProfile
    from balatro_bot.strategy import Strategy

# Hand types that care about hand size (need 4-5 scoring cards or consecutive ranks)
_FIVE_CARD_HANDS = frozenset({
    "Flush", "Straight", "Full House", "Four of a Kind",
    "Straight Flush", "Flush House", "Flush Five",
})


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

# $1 of expected net profit ≈ this many value units. Calibrated so a Golden
# Joker at ante 1 (≈$48 profit over 12 rounds) lands around 7.2 — roughly
# on par with a decent scoring joker and well above flat-$1 utility entries.
DOLLARS_PER_VALUE_UNIT = 0.15

# Late-game opportunity cost — eco payoffs lose ground to scoring pressure
# as antes tick up. By ante 6 the slot is better spent on chips/mult/xmult,
# and even perfectly-performing eco has little runway left to compound.
# Applied multiplicatively in ``dollars_to_value`` so every ROI valuator
# gets the same curve without per-joker plumbing.
ECO_ANTE_DECAY: dict[int, float] = {
    1: 1.0, 2: 1.0, 3: 0.9,
    4: 0.6, 5: 0.35, 6: 0.2, 7: 0.1, 8: 0.05,
}

# Typical hands played per blind (3 blinds per ante). Used by conditional
# trigger valuators. 3.5 reflects: ~4 hands available, usually 2-4 played.
HANDS_PER_ROUND = 3.5

# Average cards that actually score per hand (subset of 5 played). A Pair
# scores 2, Three-of-a-Kind scores 3, full-hand types score 5; 3.0 is the
# blended average across typical play.
SCORED_CARDS_PER_HAND = 3.0

# Default hand size and discards-per-round — used by jokers that reward
# held cards (Reserved Parking) or unused discards (Delayed Gratification).
HAND_SIZE_DEFAULT = 8
DISCARDS_PER_ROUND = 3
AVG_DISCARDS_USED = 1.5         # → ~1.5 unused per round
AVG_CARDS_DISCARDED = 6.0       # summed across all discards in a round


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def rounds_remaining(ante: int) -> int:
    """Rounds left in the run, inclusive of the current ante's remaining blinds.

    Matches the estimate used by ``compute_budget`` in shop_evaluator.py:
    ``(8 - ante) * 3``. Treats the current shop visit as the start of a
    new blind, so the remaining count for ante N counts rounds N..8.
    """
    return max(1, (8 - ante) * 3)


def dollars_to_value(dollars: float, ante: int = 1) -> float:
    """Convert expected net dollars to the shop_valuation value scale.

    Applies ``ECO_ANTE_DECAY[ante]`` so late-ante eco buys fall off the
    valuation cliff — by ante 6 a scoring joker in the same slot is worth
    far more than $X/round of dwindling runway.
    """
    decay = ECO_ANTE_DECAY.get(ante, 0.1)
    return max(0.0, dollars) * DOLLARS_PER_VALUE_UNIT * decay


def _face_ratio(deck_profile: DeckProfile) -> float:
    """Fraction of the deck that is J/Q/K. Falls back to vanilla 12/52."""
    if not deck_profile or deck_profile.total_cards <= 0:
        return 12.0 / 52.0
    faces = sum(deck_profile.rank_counts.get(r, 0) for r in ("J", "Q", "K"))
    return faces / deck_profile.total_cards


# ---------------------------------------------------------------------------
# Reference valuators
# ---------------------------------------------------------------------------

def _golden_dollars(ante: int, **_: object) -> float:
    """Golden Joker: flat +$4 at end of each round."""
    return 4.0 * rounds_remaining(ante)


def _business_card_dollars(
    ante: int, deck_profile: DeckProfile | None = None, **_: object
) -> float:
    """Business Card: face card scored → 1-in-2 for +$2.

    Expected $ = 0.5 × $2 × face_cards_scored_per_hand × hands × rounds.
    """
    face_ratio = _face_ratio(deck_profile) if deck_profile else 12.0 / 52.0
    face_scored_per_hand = SCORED_CARDS_PER_HAND * face_ratio
    per_hand = 0.5 * 2.0 * face_scored_per_hand
    return per_hand * HANDS_PER_ROUND * rounds_remaining(ante)


def _cloud_9_dollars(
    ante: int, deck_profile: DeckProfile | None = None, **_: object
) -> float:
    """Cloud 9: +$1 per 9 in full deck at end of round."""
    nines = deck_profile.rank_counts.get("9", 0) if deck_profile else 4
    return float(nines) * rounds_remaining(ante)


def _delayed_grat_dollars(ante: int, **_: object) -> float:
    """Delayed Gratification: +$2 per unused discard at round end."""
    unused = max(0.0, DISCARDS_PER_ROUND - AVG_DISCARDS_USED)
    return 2.0 * unused * rounds_remaining(ante)


def _rocket_dollars(ante: int, **_: object) -> float:
    """Rocket: +$1 base end-of-round, +$2 permanent gain per boss defeated.

    Approximation: current base ≈ 1 + 2*(ante-1); grows by +2 each future
    boss (one boss per remaining ante). Average base over remaining run ≈
    current_base + future_bosses. Integrate over remaining rounds.
    """
    current_base = 1.0 + 2.0 * max(0, ante - 1)
    future_bosses = max(0, 8 - ante)
    avg_base = current_base + future_bosses  # midpoint growth approximation
    return avg_base * rounds_remaining(ante)


def _to_the_moon_dollars(ante: int, **_: object) -> float:
    """To the Moon: +$1 extra interest per $5 held at end of round.

    No money threaded into evaluator yet — assume the bot sits near the
    $25 interest cap for most of the run (5 interest tiers × $1 = +$5/round).
    Early antes haven't accumulated, so dampen by ante.
    """
    tier_coverage = min(1.0, ante / 4.0)  # ramp: 0.25/0.5/0.75/1.0 over antes 1-4
    return 5.0 * tier_coverage * rounds_remaining(ante)


def _trading_card_dollars(ante: int, **_: object) -> float:
    """Trading Card: first discard of round destroys single card + $3.

    Triggers only when first discard is exactly one card — estimate 50%
    of rounds (filler discards, targeted debuff removal).
    """
    return 0.5 * 3.0 * rounds_remaining(ante)


def _reserved_parking_dollars(
    ante: int, deck_profile: DeckProfile | None = None, **_: object
) -> float:
    """Reserved Parking: face card in hand → 1-in-2 for +$1 each scoring.

    Expected per hand = 0.5 × $1 × faces_held. Faces held ≈ hand_size × face_ratio.
    """
    face_held = HAND_SIZE_DEFAULT * _face_ratio(deck_profile) if deck_profile else HAND_SIZE_DEFAULT * 12.0 / 52.0
    per_hand = 0.5 * 1.0 * face_held
    return per_hand * HANDS_PER_ROUND * rounds_remaining(ante)


def _mail_in_rebate_dollars(ante: int, **_: object) -> float:
    """Mail-In Rebate: +$5 per discarded card matching rank of the round.

    Rank rerolls each round — 1/13 match rate per discarded card.
    """
    per_round = AVG_CARDS_DISCARDED * (1.0 / 13.0) * 5.0
    return per_round * rounds_remaining(ante)


def _faceless_dollars(
    ante: int, deck_profile: DeckProfile | None = None, **_: object
) -> float:
    """Faceless: +$5 if ≥3 face cards discarded in a single discard.

    P(single discard has 3+ faces) depends on discard size and face ratio.
    With ~3 cards per discard and vanilla face rate, trigger is rare
    (<5% of discards). Conservative estimate: ~$1/round.
    """
    face_ratio = _face_ratio(deck_profile) if deck_profile else 12.0 / 52.0
    # Rough: P ~ face_ratio^2 × 1.5 (boost for larger discards)
    trigger_per_discard = min(0.15, face_ratio * face_ratio * 1.5)
    per_round = trigger_per_discard * AVG_DISCARDS_USED * 5.0
    return per_round * rounds_remaining(ante)


def _to_do_list_dollars(ante: int, **_: object) -> float:
    """To Do List: +$4 if the day's poker hand is played. Hand rerolls each round.

    Bot typically has 1-2 preferred hands; match rate ≈ 2/9 ≈ 22%.
    """
    return 0.22 * 4.0 * rounds_remaining(ante)


def _satellite_dollars(
    ante: int, unique_planets_used: int = 0, **_: object
) -> float:
    """Satellite: +$1 per unique Planet used this run, end of round.

    Value at purchase = current_unique × rounds_remaining, plus projected
    growth from future planet buys. Assume ~0.4 unique planets added per
    remaining round when the bot buys planets on strategy.
    """
    future_gain_rate = 0.4  # unique planets added per round, rough
    rr = rounds_remaining(ante)
    # Sum of (current + future_gain × round_index) over rr rounds
    # = current × rr + future_gain × rr × (rr - 1) / 2
    return unique_planets_used * rr + future_gain_rate * rr * max(0, rr - 1) / 2.0


def _egg_dollars(ante: int, **_: object) -> float:
    """Egg: +$3 sell value per round played. Realized only at sell.

    Value = $3/round × rounds_held × P(actually sold before run ends).
    Bot rarely sells Eggs proactively — heavy 0.5 discount.
    """
    return 3.0 * rounds_remaining(ante) * 0.5


def _juggler_dollars(
    ante: int,
    strategy: Strategy | None = None,
    owned_keys: frozenset[str] = frozenset(),
    **_: object,
) -> float:
    """Juggler: +1 hand size. Only meaningful for 4-5 card hand builds.

    Math: 9-card draws roughly double P(Flush/Straight/4oaK) vs 8-card draws
    and bump P(3oaK) by ~40%. Pair/High Card barely move. Decays sharply —
    it's an enabler, not a scorer, so the bot usually sells by ante 5-6.
    """
    top = strategy.top_hand() if strategy else None
    if top in _FIVE_CARD_HANDS:
        per_round = 3.0
    elif top == "Three of a Kind":
        per_round = 1.5
    else:
        per_round = 0.3

    if "j_baron" in owned_keys:
        per_round += 1.0  # more held Kings = more held xMult triggers

    # Enabler decay: strong early, quick sell mid-game
    decay = {1: 1.0, 2: 1.0, 3: 0.7, 4: 0.5, 5: 0.3}.get(ante, 0.15)
    return per_round * decay * rounds_remaining(ante)


_DISCARD_SCALERS = frozenset({
    "j_castle", "j_yorick", "j_hit_the_road", "j_mail",
})

_PREMIUM_EDITIONS = frozenset({"FOIL", "HOLO", "HOLOGRAPHIC", "POLYCHROME"})


def _count_on_strategy_scorers(
    owned_jokers: list[dict], strategy: Strategy | None
) -> tuple[int, bool]:
    """Count scorers whose hand affinity overlaps the bot's preferred hands.

    Returns (scorer_count, has_godlike). "Godlike" = xmult joker with a
    premium edition (Foil/Holo/Polychrome).
    """
    from balatro_bot.cards import joker_key
    from balatro_bot.domain.policy.shop_valuation import JOKER_SCORE_CATEGORY
    from balatro_bot.strategy import JOKER_HAND_AFFINITY

    if not strategy or not strategy.preferred_hands:
        return 0, False
    pref = {h for h, _ in strategy.preferred_hands}
    xmult_set = JOKER_SCORE_CATEGORY.get("xmult", set())

    scorers, godlike = 0, False
    for j in owned_jokers:
        k = joker_key(j)
        hands = set(JOKER_HAND_AFFINITY.get(k, ([], 0))[0])
        if hands & pref:
            scorers += 1
            mod = j.get("modifier")
            edition = mod.get("edition") if isinstance(mod, dict) else None
            if k in xmult_set and edition in _PREMIUM_EDITIONS:
                godlike = True
    return scorers, godlike


def _troubadour_dollars(
    ante: int,
    strategy: Strategy | None = None,
    owned_jokers: list[dict] | None = None,
    **_: object,
) -> float:
    """Troubadour: +2 hand size, -1 hand per round.

    Roughly triples Flush/3oaK odds vs baseline, but cuts hands budget 25%.
    Net win only once the build has enough scoring power that fewer hands
    still clear. Gated on ≥2 on-strategy scorers OR 1 godlike (xmult+edition).
    """
    top = strategy.top_hand() if strategy else None
    if top not in _FIVE_CARD_HANDS and top != "Three of a Kind":
        return 0.0  # Pair/HC: lose a hand with no probability upside

    scorers, godlike = _count_on_strategy_scorers(owned_jokers or [], strategy)
    if not (scorers >= 2 or godlike):
        return 0.0  # build not ready — -1 hand too costly

    per_round = 5.0 if top in _FIVE_CARD_HANDS else 2.5
    decay = {1: 1.0, 2: 1.0, 3: 1.0, 4: 0.9, 5: 0.7}.get(ante, 0.4)
    return per_round * decay * rounds_remaining(ante)


def _drunkard_dollars(
    ante: int,
    strategy: Strategy | None = None,
    owned_keys: frozenset[str] = frozenset(),
    **_: object,
) -> float:
    """Drunkard: +1 discard per round. Same vibe as Juggler — vague enabler.

    Helps chase-heavy builds (Flush/Straight/4oaK) that actually discard.
    Bonus when discard-scaling jokers are owned; dead under Burglar (handled
    by the anti-synergy map, not here).
    """
    top = strategy.top_hand() if strategy else None
    if top in _FIVE_CARD_HANDS:
        per_round = 2.5
    elif top == "Three of a Kind":
        per_round = 1.2
    else:
        per_round = 0.3

    # Discard-scaler synergy: extra discard = extra scaling trigger
    per_round += len(owned_keys & _DISCARD_SCALERS) * 0.8

    decay = {1: 1.0, 2: 1.0, 3: 0.7, 4: 0.5, 5: 0.3}.get(ante, 0.15)
    return per_round * decay * rounds_remaining(ante)


def _gift_card_dollars(
    ante: int, owned_count: int = 0, **_: object
) -> float:
    """Gift Card: +$1 sell value per round for every joker AND consumable.

    owned_count covers jokers; add +1 as a rough estimate for typical
    consumable slot usage. Realized only when items are sold — apply the
    same 0.5 sell-discount as Egg.
    """
    items = owned_count + 1 + 1  # +1 for Gift itself, +1 average consumable
    return 1.0 * items * rounds_remaining(ante) * 0.5


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# Each valuator returns expected net *dollars* over the remainder of the run.
# Keep signatures uniform: accept ante + deck_profile + **kwargs for future
# signals (money, owned jokers, etc.) without breaking existing entries.
UTILITY_ROI_VALUATORS: dict[str, Callable[..., float]] = {
    # Flat per-round income
    "j_golden":           _golden_dollars,
    "j_delayed_grat":     _delayed_grat_dollars,
    "j_rocket":           _rocket_dollars,
    "j_to_the_moon":      _to_the_moon_dollars,
    "j_trading":          _trading_card_dollars,
    "j_todo_list":        _to_do_list_dollars,
    # Conditional per-hand trigger
    "j_business":         _business_card_dollars,
    "j_reserved_parking": _reserved_parking_dollars,
    "j_mail":             _mail_in_rebate_dollars,
    "j_faceless":         _faceless_dollars,
    # Deck-state-scaled
    "j_cloud_9":          _cloud_9_dollars,
    # Run-state-scaled
    "j_satellite":        _satellite_dollars,
    # Sell-value ramps (delayed payout, discounted)
    "j_egg":              _egg_dollars,
    "j_gift":             _gift_card_dollars,
    # Hand-size / resource jokers
    "j_juggler":          _juggler_dollars,
    "j_drunkard":         _drunkard_dollars,
    "j_troubadour":       _troubadour_dollars,
}


def evaluate(
    key: str,
    ante: int,
    deck_profile: DeckProfile | None = None,
    owned_count: int = 0,
    unique_planets_used: int = 0,
    strategy: Strategy | None = None,
    owned_keys: frozenset[str] = frozenset(),
    owned_jokers: list[dict] | None = None,
) -> float | None:
    """Return value-scale score for *key*, or None if no ROI valuator exists."""
    valuator = UTILITY_ROI_VALUATORS.get(key)
    if valuator is None:
        return None
    dollars = valuator(
        ante=ante,
        deck_profile=deck_profile,
        owned_count=owned_count,
        unique_planets_used=unique_planets_used,
        strategy=strategy,
        owned_keys=owned_keys,
        owned_jokers=owned_jokers or [],
    )
    return dollars_to_value(dollars, ante=ante)
