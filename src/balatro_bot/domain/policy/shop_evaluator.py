"""Unified shop evaluator — scores everything, plans a budget, ranks actions.

Replaces the siloed shop rules (SellWeakJoker, BuyJokersInShop, BuyPacksInShop,
BuyVouchersInShop, BuyConsumablesInShop, FeedCampfire, SellDietCola,
ReorderJokersForScoring, RerollShop, LeaveShop, SellInvisible) with one
system that evaluates composite action plans and picks the best one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from balatro_bot.actions import (
    Action, BuyCard, BuyPack, BuyVoucher, NextRound, Reroll,
    RearrangeJokers, SellConsumable, SellJoker,
)
from balatro_bot.cards import joker_key
from balatro_bot.constants import (
    PLANET_KEYS, SAFE_CONSUMABLE_TAROTS, SAFE_SPECTRAL_CONSUMABLES,
    SPECTRAL_TARGETING, TARGETING_TAROTS,
)
from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.policy.shop_valuation import (
    _KEY_TO_CATEGORY, evaluate_joker_value, parse_effect_value,
)
from balatro_bot.joker_effects.scoring_phase import (
    get_joker_phase, get_joker_edition_phase,
    PHASE_NOOP, PHASE_CHIPS, PHASE_MULT, PHASE_XMULT,
)
from balatro_bot.rules._helpers import score_consumable, evaluate_hex
from balatro_bot.scaling import check_anti_synergy
from balatro_bot.strategy import compute_strategy

if TYPE_CHECKING:
    from typing import Any

    from balatro_bot.strategy import Strategy

log = logging.getLogger("balatro_bot")

# ---------------------------------------------------------------------------
# Economy phases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Budget:
    """Economy plan for this shop visit.

    Two aggression dials separate concerns that used to share one knob:
      - scoring_aggression: how eagerly to buy scoring power (jokers,
        planets/tarots, Celestial/Buffoon packs, Campfire feed). High early
        because building scoring power is the whole point of the early game.
      - speculative_aggression: how eagerly to make variance spends (rerolls,
        vouchers, Arcana/Spectral packs, Diet Cola sell). Low early because
        these trade real money for uncertain EV and break interest stacking.
    """
    phase: str                       # "BUILD", "FLEX", "SPEND"
    reserve: int                     # money floor — don't dip below
    spend_ceiling: int               # max we'll spend this visit
    scoring_aggression: float        # 0.0-1.0 — buy scoring power
    speculative_aggression: float    # 0.0-1.0 — speculative spends
    rounds_est: int                  # estimated rounds remaining
    # Voucher-derived knobs
    interest_cap_per_round: int = 5  # 5 / 10 / 20 (Seed Money / Money Tree)
    reroll_cost: int = 5             # 5 / 3 / 1 (Reroll Surplus / Glut)
    reroll_cap: int = 3              # 3 / 5 / 8 (Reroll Surplus / Glut)
    shop_slot_bonus: int = 0         # 0 / 1 / 2 (Overstock / Plus)


def compute_budget(
    money: int, ante: int, joker_count: int = 0,
    owned_jokers: list[dict] | None = None,
    owned_vouchers: set[str] | None = None,
) -> Budget:
    """Determine economy phase and spending limits.

    The reserve is a *soft target*, not a hard floor. The spend_ceiling
    ensures the bot can always spend something meaningful — it just costs
    more in opportunity cost when dipping below reserve (handled by
    _money_opportunity_cost, which uses speculative_aggression).
    """
    rounds_est = max(1, (8 - ante) * 3)

    if ante <= 2:
        # Antes 1-2: build scoring aggressively, but don't burn cash on
        # rerolls or $10 vouchers — interest stacking still matters once
        # the roster has anything to protect.
        phase, reserve = "BUILD", 0
        scoring_agg, speculative_agg = 1.0, 0.3
    elif ante <= 3:
        phase, reserve = "BUILD", 15
        scoring_agg, speculative_agg = 0.8, 0.5
    elif ante <= 5:
        phase, reserve = "FLEX", 10
        scoring_agg, speculative_agg = 0.7, 0.7
    else:
        phase, reserve = "SPEND", 5
        scoring_agg, speculative_agg = 1.0, 1.0

    # Empty slots override: with open joker slots the bot MUST buy to scale.
    # Each empty slot below 3 owned pushes scoring aggression toward 1.0.
    # Speculative aggression is NOT boosted — empty slots don't make rerolls
    # or vouchers any better.
    #
    # Exception: if chip AND mult phases are already covered at ante ≤2,
    # empty slots don't justify max aggression — the roster is already
    # sufficient for early blinds. Cap slot aggression at 0.85 in that case.
    if joker_count < 3:
        slot_aggression = 1.0 - joker_count * 0.15  # 1.0, 0.85, 0.7
        if ante <= 2 and owned_jokers:
            owned_cats = {
                _KEY_TO_CATEGORY.get(joker_key(j)) for j in owned_jokers
            }
            if "chips" in owned_cats and ("mult" in owned_cats or "xmult" in owned_cats):
                slot_aggression = min(slot_aggression, 0.85)
        scoring_agg = max(scoring_agg, slot_aggression)
        reserve = min(reserve, 5)  # relax reserve when roster is thin

    # Early game: don't let reserve block all spending
    # Keep at least half of money available for purchases
    # The opportunity cost function penalizes interest-breaking, so this
    # won't cause reckless spending — just allows it when EV justifies it
    spend_ceiling = max(money // 2, money - reserve)

    # No jokers yet: must buy something, ignore economy constraints.
    # Speculative aggression stays put — we want jokers, not rerolls.
    if joker_count == 0:
        spend_ceiling = money
        reserve = 0
        scoring_agg = 1.0

    # Voucher effects on economy knobs.
    vouchers = owned_vouchers or set()
    if "v_money_tree" in vouchers:
        interest_cap_per_round = 20
    elif "v_seed_money" in vouchers:
        interest_cap_per_round = 10
    else:
        interest_cap_per_round = 5

    if "v_reroll_glut" in vouchers:
        reroll_cost, reroll_cap = 1, 8
    elif "v_reroll_surplus" in vouchers:
        reroll_cost, reroll_cap = 3, 5
    else:
        reroll_cost, reroll_cap = 5, 3

    if "v_overstock_plus" in vouchers:
        shop_slot_bonus = 2
    elif "v_overstock" in vouchers:
        shop_slot_bonus = 1
    else:
        shop_slot_bonus = 0

    # Clearance Sale / Liquidation: cheaper items mean each dollar buys more
    # scoring power, so nudge scoring aggression up.
    if "v_liquidation" in vouchers:
        scoring_agg = min(1.0, scoring_agg * 1.2)
    elif "v_clearance_sale" in vouchers:
        scoring_agg = min(1.0, scoring_agg * 1.1)

    return Budget(
        phase, reserve, spend_ceiling, scoring_agg, speculative_agg, rounds_est,
        interest_cap_per_round=interest_cap_per_round,
        reroll_cost=reroll_cost,
        reroll_cap=reroll_cap,
        shop_slot_bonus=shop_slot_bonus,
    )


# ---------------------------------------------------------------------------
# Roster scoring — live EV delta per owned joker
# ---------------------------------------------------------------------------

@dataclass
class JokerScore:
    """Live valuation of an owned joker."""
    index: int
    key: str
    label: str
    ev_delta: float     # scoring contribution (with vs without)
    sell_value: int      # cash from selling
    fodder_mult: float   # mult Dagger would gain if this joker is eaten
    effective_value: float  # max(ev_delta, fodder_value) for ranking


def score_roster(
    owned: list[dict],
    hand_levels: dict,
    strategy: Strategy,
    joker_limit: int = 5,
    deck_profile: DeckProfile | None = None,
    ante: int = 1,
) -> list[JokerScore]:
    """Compute live EV delta for each owned joker."""
    scores: list[JokerScore] = []

    # Check if Ceremonial Dagger is owned (for fodder scoring)
    dagger_owned = any(joker_key(j) == "j_ceremonial" for j in owned)

    for i, j in enumerate(owned):
        key = joker_key(j)
        label = j.get("label", "?")
        sell_value = j.get("cost", {}).get("sell", 0)

        # EV delta: how much does removing this joker hurt scoring?
        ev = evaluate_joker_value(
            j, owned_jokers=owned, hand_levels=hand_levels,
            ante=ante, strategy=strategy, joker_limit=joker_limit,
            deck_profile=deck_profile,
        )

        # Fodder value: if Dagger is owned and this isn't Dagger itself,
        # compute how much mult Dagger gains from eating this joker
        # (2x sell value as permanent +mult)
        fodder_mult = 0.0
        if dagger_owned and key != "j_ceremonial":
            fodder_mult = sell_value * 2.0

        # Effective value: a joker is worth at least its fodder value to Dagger
        # (if it's more valuable as food than as a scorer, mark it for feeding)
        effective = max(ev, fodder_mult) if dagger_owned else ev

        scores.append(JokerScore(
            index=i, key=key, label=label,
            ev_delta=ev, sell_value=sell_value,
            fodder_mult=fodder_mult, effective_value=effective,
        ))

    return scores


# ---------------------------------------------------------------------------
# Action plans
# ---------------------------------------------------------------------------

@dataclass
class ActionPlan:
    """A ranked candidate action (possibly multi-step)."""
    steps: list[Action]
    net_value: float    # EV improvement minus cost opportunity
    description: str


# ---------------------------------------------------------------------------
# Reorder computation (ported from ReorderJokersForScoring)
# ---------------------------------------------------------------------------

def _compute_optimal_order(
    owned: list[dict],
    fodder_idx: int | None = None,
) -> list[int] | None:
    """Compute optimal joker order. Returns new index list or None if already optimal.

    If fodder_idx is provided, that joker is positioned right of Ceremonial Dagger.
    """
    if len(owned) < 2:
        return None

    ceremonial_idx: int | None = None
    blueprint_idx: int | None = None
    brainstorm_idx: int | None = None
    noop_fodder: list[int] = []

    for i, j in enumerate(owned):
        key = joker_key(j)
        phase = get_joker_phase(key)
        if key == "j_ceremonial":
            ceremonial_idx = i
        elif key == "j_blueprint":
            blueprint_idx = i
        elif key == "j_brainstorm":
            brainstorm_idx = i
        if phase == PHASE_NOOP:
            noop_fodder.append(i)

    # If a specific fodder_idx is requested, add it to fodder candidates
    # even if it's not PHASE_NOOP (it's dead weight identified by live EV)
    if fodder_idx is not None and fodder_idx not in noop_fodder:
        noop_fodder.append(fodder_idx)

    excluded = {blueprint_idx, brainstorm_idx} - {None}
    sortable = []
    for i, j in enumerate(owned):
        if i in excluded:
            continue
        key = joker_key(j)
        phase = get_joker_phase(key)
        ed_phase = get_joker_edition_phase(j)
        sortable.append((i, phase, ed_phase))

    sortable.sort(key=lambda x: (x[1], x[2], x[0]))
    desired_order = [i for i, _, _ in sortable]

    # Ceremonial Dagger constraint
    if ceremonial_idx is not None:
        available_fodder = [f for f in noop_fodder if f != ceremonial_idx
                            and f not in excluded]
        # Also consider the explicit fodder_idx
        if fodder_idx is not None and fodder_idx not in available_fodder and fodder_idx != ceremonial_idx:
            available_fodder.append(fodder_idx)

        desired_order = [i for i in desired_order if i != ceremonial_idx]
        if available_fodder:
            # Prefer the explicitly requested fodder, else first available
            if fodder_idx is not None and fodder_idx in available_fodder:
                sacrifice_idx = fodder_idx
            else:
                available_fodder.sort(key=lambda i: joker_key(owned[i]))
                sacrifice_idx = available_fodder[0]
            desired_order = [i for i in desired_order if i != sacrifice_idx]
            insert_at = 0
            for pos, idx in enumerate(desired_order):
                if get_joker_phase(joker_key(owned[idx])) <= PHASE_MULT:
                    insert_at = pos + 1
            desired_order.insert(insert_at, ceremonial_idx)
            desired_order.insert(insert_at + 1, sacrifice_idx)
        else:
            desired_order.append(ceremonial_idx)

    # Blueprint: left of best xmult target
    if blueprint_idx is not None:
        best_copy_pos = None
        for pos, idx in enumerate(desired_order):
            phase = get_joker_phase(joker_key(owned[idx]))
            if phase == PHASE_XMULT:
                best_copy_pos = pos
            elif phase == PHASE_MULT and best_copy_pos is None:
                best_copy_pos = pos
        if best_copy_pos is not None:
            desired_order.insert(best_copy_pos, blueprint_idx)
        else:
            desired_order.append(blueprint_idx)

    # Brainstorm: at end, ensure slot 0 is good
    if brainstorm_idx is not None:
        desired_order.append(brainstorm_idx)
        if desired_order and get_joker_phase(joker_key(owned[desired_order[0]])) == PHASE_NOOP:
            for pos in range(1, len(desired_order)):
                if get_joker_phase(joker_key(owned[desired_order[pos]])) != PHASE_NOOP:
                    desired_order[0], desired_order[pos] = desired_order[pos], desired_order[0]
                    break

    # Check if order actually changed
    if desired_order == list(range(len(owned))):
        return None
    return desired_order


# ---------------------------------------------------------------------------
# Shop item scoring
# ---------------------------------------------------------------------------

def _score_shop_joker(
    card: dict,
    owned: list[dict],
    hand_levels: dict,
    strategy: Strategy,
    ante: int,
    joker_limit: int,
    deck_profile: DeckProfile | None,
) -> float:
    """Score a shop joker using the valuation engine."""
    return evaluate_joker_value(
        card, owned_jokers=owned, hand_levels=hand_levels,
        ante=ante, strategy=strategy, joker_limit=joker_limit,
        deck_profile=deck_profile,
    )


def _score_pack(
    label: str, money: int, owned_jokers: list[dict], ante: int,
    joker_limit: int = 5,
) -> float:
    """Heuristic value for a pack."""
    from balatro_bot.scaling import red_card_skip_value

    has_red_card = any(joker_key(j) == "j_red_card" for j in owned_jokers)
    has_constellation = any(joker_key(j) == "j_constellation" for j in owned_jokers)

    # Early-ante pack deflator: at ante ≤2 the bot has little scoring roster to
    # amplify pack picks, so realized value of a random planet/tarot is much
    # lower than the raw heuristic suggests. Packs were the biggest cash leak
    # in the ante 1-2 data (1.8/ante vs 1.4 jokers/ante).
    early = ante <= 2

    # Celestial and Buffoon: SkipPackForRedCard exempts these (planets/jokers),
    # so Red Card never fires — no skip bonus.
    if "Celestial" in label:
        if has_constellation:
            return 9.0
        return 3.0 if early else 6.0
    if "Buffoon" in label:
        if len(owned_jokers) >= joker_limit:
            return 0.0
        return 2.5 if early else 4.0

    # Packs Red Card will skip — add skip bonus on top of pick value.
    if "Arcana" in label:
        base = 1.5 if early else 3.0
    elif "Spectral" in label:
        base = 2.0 if ante >= 3 else 0.3
    elif "Standard" in label:
        base = 1.0
    else:
        return 0.0

    if has_red_card:
        base += red_card_skip_value(ante)
    return base


_VOUCHER_ROI: dict[str, float] = {
    # Hands/discards: direct scoring impact
    "v_grabber": 12.0, "v_nacho_tong": 12.0,
    "v_paint_brush": 8.0, "v_palette": 8.0,
    "v_wasteful": 6.0, "v_recyclomancy": 6.0,
    # Joker slots
    "v_blank": 0.5,  # prereq only — no direct effect
    "v_antimatter": 7.0,
    # Consumable slots / pack modifiers
    "v_crystal_ball": 3.0,
    "v_omen_globe": 2.0,  # spectrals can appear in arcana packs
    # Planet / tarot economy
    "v_telescope": 10.0,  # free planet matching most-played hand every shop
    "v_observatory": 5.0,  # X1.5 mult for owned planet consumables when used
    "v_tarot_merchant": 3.0, "v_tarot_tycoon": 4.0,
    "v_planet_merchant": 3.0, "v_planet_tycoon": 4.0,
    # Editions / shop card variety
    "v_hone": 4.0, "v_glow_up": 6.0,
    "v_magic_trick": 2.0, "v_illusion": 3.0,
    # Rerolls
    "v_reroll_surplus": 4.0, "v_reroll_glut": 5.0,
    # Boss reroll
    "v_directors_cut": 2.0, "v_retcon": 4.0,
    # Ante skip
    "v_hieroglyph": 5.0, "v_petroglyph": 5.0,
    # Interest cap — computed dynamically from rounds_est
    "v_seed_money": 0.0,
    "v_money_tree": 0.0,
    # Shop improvements
    "v_clearance_sale": 2.0, "v_liquidation": 4.0,
    "v_overstock": 2.0, "v_overstock_plus": 3.0,
}

# Tier-2 → tier-1 prereq. Tier-2 vouchers only appear when prereq is owned, so
# seeing one in the shop means the chain is already in flight — completing it
# is a strictly-better ROI than breaking it.
_VOUCHER_PREREQ: dict[str, str] = {
    "v_nacho_tong": "v_grabber",
    "v_palette": "v_paint_brush",
    "v_recyclomancy": "v_wasteful",
    "v_antimatter": "v_blank",
    "v_omen_globe": "v_crystal_ball",
    "v_observatory": "v_telescope",
    "v_tarot_tycoon": "v_tarot_merchant",
    "v_planet_tycoon": "v_planet_merchant",
    "v_glow_up": "v_hone",
    "v_illusion": "v_magic_trick",
    "v_reroll_glut": "v_reroll_surplus",
    "v_retcon": "v_directors_cut",
    "v_petroglyph": "v_hieroglyph",
    "v_money_tree": "v_seed_money",
    "v_liquidation": "v_clearance_sale",
    "v_overstock_plus": "v_overstock",
}


def _score_voucher(
    key: str,
    cost: int,
    money: int,
    budget: Budget,
    owned_vouchers: set[str],
) -> float:
    """Score a voucher by ROI over remaining rounds."""
    base = _VOUCHER_ROI.get(key)
    if base is None:
        return 0.0

    # Economy vouchers: value scales with rounds remaining
    if key == "v_seed_money":
        # Raises interest cap from $5/round to $10/round
        base = min(5.0 * budget.rounds_est, 30.0)
    elif key == "v_money_tree":
        # Raises interest cap to $20/round
        base = min(10.0 * budget.rounds_est, 50.0)

    # Scale by remaining rounds for non-economy vouchers too
    if key not in ("v_seed_money", "v_money_tree"):
        base *= min(budget.rounds_est / 10.0, 1.5)

    # Chain completion bonus: tier-2 upgrades strictly improve a chain already
    # sunk into — don't let marginal opp_cost block completing it.
    prereq = _VOUCHER_PREREQ.get(key)
    if prereq is not None and prereq in owned_vouchers:
        base *= 1.3

    return base


# ---------------------------------------------------------------------------
# Consumable sell scoring (Campfire feeding)
# ---------------------------------------------------------------------------

def _campfire_sell_candidates(
    consumables: list[dict],
    owned_jokers: list[dict],
    hand_levels: dict,
    ante: int,
) -> list[tuple[int, str, float]]:
    """Return (index, label, feed_value) for consumables worth selling to Campfire.

    Returns empty list if Campfire is not owned.
    """
    if not any(joker_key(j) == "j_campfire" for j in owned_jokers):
        return []

    strat = compute_strategy(owned_jokers, hand_levels)
    all_useful = (
        SAFE_CONSUMABLE_TAROTS
        | set(TARGETING_TAROTS)
        | SAFE_SPECTRAL_CONSUMABLES
        | set(SPECTRAL_TARGETING)
    )

    candidates = []
    for i, card in enumerate(consumables):
        key = card.get("key", "")
        label = card.get("label", "?")

        # Off-strategy planets: worth selling
        if key in PLANET_KEYS:
            hand_type = PLANET_KEYS[key]
            if hand_type == "ALL":
                continue
            if strat.hand_affinity(hand_type) > 0:
                continue
            candidates.append((i, label, 3.0))  # Campfire +X0.25 per sell
            continue

        # Hex: sell if not worth using
        if key == "c_hex":
            hex_score = evaluate_hex(owned_jokers, ante, hand_levels)
            if hex_score <= 0.0:
                candidates.append((i, label, 3.0))
            continue

        # Useful consumables: keep
        if key in all_useful:
            continue

        # Everything else: sell for Campfire
        candidates.append((i, label, 3.0))

    return candidates


# ---------------------------------------------------------------------------
# SellInvisible logic (multi-tick)
# ---------------------------------------------------------------------------

_COPY_JOKERS = frozenset({"j_blueprint", "j_brainstorm"})
_DUPE_EXTRA = frozenset({
    "j_vampire", "j_hologram", "j_lucky_cat", "j_canio", "j_obelisk",
    "j_yorick", "j_hit_the_road",
    "j_card_sharp", "j_seeing_double",
    "j_blueprint", "j_brainstorm",
})
# Previous ALWAYS_BUY / HIGH_PRIORITY sets — used only for dupe worthiness
_DUPE_WORTHY = frozenset({
    "j_cavendish", "j_stencil",
    "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
    "j_gros_michel", "j_popcorn",
    "j_acrobat", "j_blackboard", "j_flower_pot",
    "j_madness",
    "j_constellation", "j_campfire",
}) | _DUPE_EXTRA


def _score_dupe_target(joker: dict) -> float:
    """Score how valuable this joker is as a dupe target."""
    key = joker_key(joker)
    if key in _COPY_JOKERS:
        return 15.0
    effect_text = joker.get("value", {}).get("effect", "")
    parsed = parse_effect_value(effect_text) if effect_text else {}
    if parsed.get("xmult"):
        return parsed["xmult"] * 3.0
    if parsed.get("mult"):
        return parsed["mult"] / 5.0
    return 2.0


def _invisible_plan(
    owned: list[dict],
    ante: int,
    round_num: int,
    first_seen_round: int | None,
    selling_down: bool,
) -> tuple[ActionPlan | None, int | None, bool]:
    """Evaluate SellInvisible opportunity.

    Returns (plan_or_None, updated_first_seen_round, updated_selling_down).
    """
    invisible_idx = next(
        (i for i, j in enumerate(owned) if joker_key(j) == "j_invisible"), None
    )
    if invisible_idx is None:
        return None, None, False

    if first_seen_round is None:
        return None, round_num, False

    if round_num - first_seen_round < 2:
        return None, first_seen_round, False

    if len(owned) < 2:
        return None, first_seen_round, False

    # Find best dupe target
    best_idx = None
    best_score = -1.0
    for i, j in enumerate(owned):
        if i == invisible_idx:
            continue
        if joker_key(j) not in _DUPE_WORTHY:
            continue
        score = _score_dupe_target(j)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx is None:
        return None, first_seen_round, False

    # Ante-scaled threshold
    threshold = 3.0 if ante <= 3 else 3.0 + (ante - 3) * 1.5
    if best_score < threshold:
        return None, first_seen_round, False

    # Don't start selling before boss blind
    round_in_ante = ((round_num - 1) % 3) + 1
    boss_next = round_in_ante == 2
    if not selling_down and boss_next:
        return None, first_seen_round, False

    target_label = owned[best_idx].get("label", "?")

    # Final step: sell Invisible
    if len(owned) == 2:
        plan = ActionPlan(
            steps=[SellJoker(invisible_idx,
                             reason=f"Invisible: guaranteed dupe of {target_label} "
                                    f"(score={best_score:.1f}, ante {ante})")],
            net_value=best_score * 10,  # duping is extremely high value
            description=f"Invisible dupe: {target_label}",
        )
        return plan, first_seen_round, False

    # Sell worst non-target non-Invisible joker
    worst_idx = None
    worst_score = float("inf")
    for i, j in enumerate(owned):
        if i in (invisible_idx, best_idx):
            continue
        sell_val = j.get("cost", {}).get("sell", 0)
        effect_text = j.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect_text) if effect_text else {}
        score = float(sell_val)
        if parsed.get("xmult") and parsed["xmult"] > 1.0:
            score += parsed["xmult"] * 10
        elif parsed.get("mult") and parsed["mult"] > 0:
            score += parsed["mult"]
        if score < worst_score:
            worst_score = score
            worst_idx = i

    if worst_idx is None:
        return None, first_seen_round, selling_down

    fodder_label = owned[worst_idx].get("label", "?")
    remaining = len(owned) - 2
    plan = ActionPlan(
        steps=[SellJoker(worst_idx,
                         reason=f"Invisible setup: sell {fodder_label} to isolate "
                                f"{target_label} ({remaining} more to go, "
                                f"score={best_score:.1f})")],
        net_value=best_score * 5,  # high priority — commit to sequence
        description=f"Invisible sell-down: selling {fodder_label}",
    )
    return plan, first_seen_round, True


# ---------------------------------------------------------------------------
# Money cost opportunity
# ---------------------------------------------------------------------------

def _money_opportunity_cost(cost: int, money: int, budget: Budget,
                            category_aggression: float,
                            joker_count: int) -> float:
    """How much is spending $cost worth in lost future interest?

    Scoped to the category making the purchase: a joker buy at high
    scoring_aggression has low opp_cost (we *want* to break interest for
    scoring), while a voucher buy at low speculative_aggression pays the
    full interest tax. Without this scoping, a high speculative_aggression
    would subsidize speculation and a low one would block scoring — both
    wrong.

    The horizon is capped by roster strength: future interest only matters
    if we survive to collect it, and survival requires scoring power. With
    0 jokers the effective horizon is 3 rounds, not 21 — otherwise the
    ante 1 opp_cost dominates any realistic shop_value and the bot skips
    every affordable joker.
    """
    cap = budget.interest_cap_per_round
    current_interest = min(money // 5, cap)
    after_interest = min((money - cost) // 5, cap)
    lost_per_round = current_interest - after_interest

    effective_rounds = min(budget.rounds_est, 3 + joker_count * 4)

    # Minimum 0.2 floor so even at full aggression interest-breaking
    # buys pay a small tax. Without this, ante 1-2 buys are literally
    # free (cost_factor = 1 - 1.0 = 0) and the bot never banks cash —
    # tune data shows ~0% of ante 1-2 rounds start at interest cap.
    cost_factor = max(0.2, 1.0 - category_aggression)
    return lost_per_round * effective_rounds * cost_factor


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

class ShopEvaluator:
    """Unified shop decision engine."""

    def __init__(self) -> None:
        self.pending_plan: list[Action] | None = None
        self._pending_sell_keys: dict[int, str] = {}  # idx → expected joker key
        self._last_order: list[int] | None = None
        self._rerolls_this_shop: int = 0
        self._last_round: int = -1
        self.slots_full: bool = False  # set by bot.py on "slots are full" API error
        # SellInvisible state
        self._invisible_first_seen: int | None = None
        self._invisible_selling_down: bool = False

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        """Main entry point — called once per shop tick."""
        round_num = state.get("round_num", 0)

        # Reset per-shop state on new shop visit
        if round_num != self._last_round:
            self._rerolls_this_shop = 0
            self._last_round = round_num
            self.slots_full = False
            self.pending_plan = None
            self._pending_sell_keys = {}
            self._last_order = None

        # If mid-sequence (e.g., SellInvisible sell-down), validate and continue
        if self.pending_plan:
            if self._plan_still_valid(state):
                action = self.pending_plan.pop(0)
                if not self.pending_plan:
                    self.pending_plan = None
                return action
            else:
                log.info("[SHOP] pending plan invalidated, re-evaluating")
                self.pending_plan = None
                self._pending_sell_keys = {}

        # ── Gather state ──
        money = state.get("money", 0)
        ante = state.get("ante_num", 1)
        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])
        joker_limit = joker_info.get("limit", 5)
        joker_count = joker_info.get("count", 0)
        hand_levels = state.get("hands", {})
        shop = state.get("shop", {})
        packs = state.get("packs", {})
        vouchers = state.get("vouchers", {})
        used_vouchers_raw = state.get("used_vouchers", {})
        owned_vouchers: set[str] = (
            set(used_vouchers_raw.keys()) if isinstance(used_vouchers_raw, dict) else set()
        )
        consumables_info = state.get("consumables", {})
        consumables = consumables_info.get("cards", [])

        strat = compute_strategy(owned, hand_levels)
        deck_profile = self._get_deck_profile(state)
        budget = compute_budget(money, ante, joker_count, owned_jokers=owned,
                                owned_vouchers=owned_vouchers)

        # ── Score roster ──
        roster = score_roster(
            owned, hand_levels, strat,
            joker_limit=joker_limit, deck_profile=deck_profile, ante=ante,
        )

        # ── Enumerate all candidate plans ──
        candidates: list[ActionPlan] = []

        # 0. SellInvisible (highest priority multi-tick sequence)
        inv_plan, inv_first, inv_selling = _invisible_plan(
            owned, ante, round_num,
            self._invisible_first_seen, self._invisible_selling_down,
        )
        self._invisible_first_seen = inv_first
        self._invisible_selling_down = inv_selling
        if inv_plan is not None:
            candidates.append(inv_plan)

        # 1. Baseline: do nothing and leave
        # This is a LOW floor — actual interest preservation is handled by
        # _money_opportunity_cost on each purchase. The baseline just ensures
        # the bot leaves when nothing is worth buying at all.
        # Scale with inverse speculative aggression: when speculation is cheap
        # (BUILD), leaving is more attractive than gambling on the shop.
        leave_value = 0.5 * max(0.0, 1.0 - budget.speculative_aggression)
        candidates.append(ActionPlan(
            steps=[NextRound(reason="done shopping")],
            net_value=leave_value,
            description="leave shop",
        ))

        # 2. Buy jokers
        # Use len(owned) as ground truth — joker_count from API can be stale.
        # Also respect slots_full flag set by bot.py when API rejected a buy.
        actual_count = max(joker_count, len(owned))
        slots_open = actual_count < joker_limit and not self.slots_full
        if joker_count != len(owned):
            log.warning("[SHOP] joker_count mismatch: API count=%d, len(cards)=%d, limit=%d",
                        joker_count, len(owned), joker_limit)
        weakest = min(roster, key=lambda r: r.ev_delta) if roster else None

        for i, card in enumerate(shop.get("cards", [])):
            if card.get("set") != "JOKER":
                continue
            key = card.get("key", "")
            label = card.get("label", "?")
            cost = card.get("cost", {}).get("buy", 999)

            if cost > money:
                continue

            # Anti-synergy gate
            conflict = check_anti_synergy(key, {joker_key(j) for j in owned})
            if conflict:
                continue

            shop_value = _score_shop_joker(
                card, owned, hand_levels, strat, ante, joker_limit, deck_profile,
            )

            # Riff-Raff slot reservation: penalize buys that fill spawn slots
            riff_raff_owned = any(joker_key(j) == "j_riff_raff" for j in owned)
            free_slots = joker_limit - joker_count
            is_negative = _get_edition(card) == "NEGATIVE"
            if riff_raff_owned and free_slots <= 2 and not is_negative:
                shop_value *= 0.5
            if is_negative:
                shop_value = max(shop_value, 10.0)  # free slot is always high value

            if slots_open or (is_negative and actual_count < joker_limit):
                # Direct buy — Negative jokers don't consume a slot after
                # purchase, but the game still requires count < limit at buy
                # time.  When count is already at limit (from a prior Negative),
                # fall through to the sell-then-buy path below.
                opp_cost = _money_opportunity_cost(cost, money, budget,
                                                   budget.scoring_aggression,
                                                   joker_count)
                net = shop_value * budget.scoring_aggression - opp_cost
                if cost <= budget.spend_ceiling or is_negative or shop_value >= 8.0:
                    steps = [BuyCard(i, reason=f"buy {label} (${cost}, value={shop_value:.1f})")]
                    candidates.append(ActionPlan(
                        steps=steps, net_value=net,
                        description=f"buy {label}",
                    ))

            elif weakest is not None:
                # Sell weakest + buy (slots full).
                # For Negative jokers this is especially valuable: selling
                # frees a slot for the buy check, but the Negative doesn't
                # consume the slot — net result is no joker lost.
                sell_candidates = sorted(roster, key=lambda r: r.ev_delta)
                for sc in sell_candidates:
                    if _get_edition(owned[sc.index]) == "POLYCHROME":
                        continue
                    sell_cash = sc.sell_value
                    effective_cost = cost - sell_cash
                    if effective_cost > money:
                        continue
                    if is_negative:
                        # Negative: sell weakest then buy = net +1 joker.
                        # Value = the Negative's value (we keep everything
                        # except the sold joker, which comes back as a free
                        # slot once the Negative lands).
                        upgrade_delta = shop_value
                        shop_value_post = shop_value
                    else:
                        # Re-score the shop candidate against the post-sell
                        # roster so synergies/amplification from the joker
                        # we're about to sell don't inflate the buyer.
                        post_sell_owned = owned[:sc.index] + owned[sc.index + 1:]
                        post_sell_strategy = compute_strategy(post_sell_owned, hand_levels)
                        shop_value_post = _score_shop_joker(
                            card, post_sell_owned, hand_levels,
                            post_sell_strategy, ante, joker_limit, deck_profile,
                        )
                        upgrade_delta = shop_value_post - sc.ev_delta
                    opp_cost = _money_opportunity_cost(max(0, effective_cost), money, budget,
                                                       budget.scoring_aggression,
                                                       joker_count)
                    # Discount 30%: only the sell emits this tick, buy is uncertain
                    net = upgrade_delta * budget.scoring_aggression * 0.7 - opp_cost
                    if net > 0:
                        steps = [
                            SellJoker(sc.index, reason=f"sell {sc.label} (ev={sc.ev_delta:.1f}) for {'Negative buy' if is_negative else f'{label} (post-sell value={shop_value_post:.1f})'}"),
                        ]
                        # After sell, the shop card index is still i but joker indices shift
                        # The engine re-evaluates each tick, so we just emit the sell
                        # and re-evaluate next tick for the buy
                        candidates.append(ActionPlan(
                            steps=steps, net_value=net,
                            description=f"sell {sc.label} → buy {label}",
                        ))
                    break  # only consider the single weakest

        # 3. Dagger fodder positioning (no sell needed — just reorder)
        dagger_owned = any(joker_key(j) == "j_ceremonial" for j in owned)
        if dagger_owned and roster:
            # Find jokers where ev_delta ≈ 0 but fodder_mult > 0
            for r in roster:
                if r.key == "j_ceremonial":
                    continue
                if r.ev_delta < 1.0 and r.fodder_mult > 0:
                    # This joker is worth more as Dagger food
                    new_order = _compute_optimal_order(owned, fodder_idx=r.index)
                    if new_order is not None and new_order != self._last_order:
                        candidates.append(ActionPlan(
                            steps=[RearrangeJokers(order=new_order,
                                                   reason=f"feed {r.label} to Dagger (+{r.fodder_mult:.0f} mult)")],
                            net_value=r.fodder_mult * budget.scoring_aggression,
                            description=f"feed {r.label} to Dagger",
                        ))

        # 4. General reorder (scoring optimization)
        # Skip whenever a Dagger+fodder pairing exists on the roster, even if
        # no feed plan is live this tick. After a feed the fodder sits right of
        # Dagger and the plan goes silent (new_order == _last_order), but a
        # scoring reorder would shuffle the fodder back left of Dagger, which
        # re-triggers the feed plan next tick — infinite loop.
        has_dagger_fodder = dagger_owned and any(
            r.key != "j_ceremonial" and r.ev_delta < 1.0 and r.fodder_mult > 0
            for r in roster
        )
        if owned and not has_dagger_fodder:
            new_order = _compute_optimal_order(owned)
            if new_order is not None and new_order != self._last_order:
                phase_names = {PHASE_NOOP: "noop", PHASE_CHIPS: "+c", PHASE_MULT: "+m", PHASE_XMULT: "xm"}
                order_desc = " ".join(
                    f"{owned[idx].get('label', '?')}({phase_names.get(get_joker_phase(joker_key(owned[idx])), '?')})"
                    for idx in new_order
                )
                candidates.append(ActionPlan(
                    steps=[RearrangeJokers(order=new_order, reason=f"scoring order: {order_desc}")],
                    net_value=2.0,  # moderate priority — order matters but isn't urgent
                    description="reorder for scoring",
                ))

        # 5. Buy consumables
        if consumables_info.get("count", 0) < consumables_info.get("limit", 2):
            for i, card in enumerate(shop.get("cards", [])):
                key = card.get("key", "")
                card_set = card.get("set", "")
                cost = card.get("cost", {}).get("buy", 999)
                label = card.get("label", "?")

                if card_set not in ("TAROT", "PLANET", "SPECTRAL") and key not in PLANET_KEYS:
                    continue
                if cost > money:
                    continue

                value = score_consumable(key, state, strat)
                if value <= 0:
                    continue

                opp_cost = _money_opportunity_cost(cost, money, budget,
                                                   budget.scoring_aggression,
                                                   joker_count)
                net = value * budget.scoring_aggression - opp_cost
                if cost <= budget.spend_ceiling or value >= 4.0:
                    candidates.append(ActionPlan(
                        steps=[BuyCard(i, reason=f"buy consumable: {label} (${cost}, value={value:.1f})")],
                        net_value=net,
                        description=f"buy {label}",
                    ))

        # 6. Buy packs
        for i, card in enumerate(packs.get("cards", [])):
            label = card.get("label", "")
            cost = card.get("cost", {}).get("buy", 999)
            if cost > money:
                continue

            value = _score_pack(label, money, owned, ante, joker_limit=joker_limit)
            if value <= 0:
                continue

            # Buffoon: skip if joker slots full and no Red Card
            has_red_card = any(joker_key(j) == "j_red_card" for j in owned)
            if "Buffoon" in label and not has_red_card and joker_count >= joker_limit:
                continue

            # Celestial/Buffoon are scoring-power packs; Arcana/Spectral are
            # speculative (random consumables with uncertain targeting value).
            if "Celestial" in label or "Buffoon" in label:
                pack_aggression = budget.scoring_aggression
            else:
                pack_aggression = budget.speculative_aggression
            opp_cost = _money_opportunity_cost(cost, money, budget, pack_aggression,
                                               joker_count)
            net = value * pack_aggression - opp_cost
            if cost <= budget.spend_ceiling or value >= 6.0:
                candidates.append(ActionPlan(
                    steps=[BuyPack(i, reason=f"buy pack: {label} (${cost}, value={value:.1f})")],
                    net_value=net,
                    description=f"buy {label}",
                ))

        # 7. Buy vouchers
        for i, card in enumerate(vouchers.get("cards", [])):
            key = card.get("key", "")
            cost = card.get("cost", {}).get("buy", 999)
            label = card.get("label", "?")

            value = _score_voucher(key, cost, money, budget, owned_vouchers)
            if value <= 0:
                continue
            if cost > money:
                continue

            opp_cost = _money_opportunity_cost(cost, money, budget,
                                               budget.speculative_aggression,
                                               joker_count)
            net = value * budget.speculative_aggression - opp_cost
            if cost <= budget.spend_ceiling or value >= 8.0:
                candidates.append(ActionPlan(
                    steps=[BuyVoucher(i, reason=f"buy voucher: {label} (${cost}, value={value:.1f})")],
                    net_value=net,
                    description=f"buy {label}",
                ))

        # 8. Campfire feeding (sell consumables)
        campfire_candidates = _campfire_sell_candidates(consumables, owned, hand_levels, ante)
        for idx, label, feed_value in campfire_candidates:
            candidates.append(ActionPlan(
                steps=[SellConsumable(idx, reason=f"Campfire: sell {label} (+X0.25 mult)")],
                net_value=feed_value * budget.scoring_aggression,
                description=f"Campfire feed: {label}",
            ))

        # 9. Diet Cola sell (free reroll)
        diet_idx = next(
            (i for i, j in enumerate(owned) if joker_key(j) == "j_diet_cola"), None
        )
        if diet_idx is not None:
            # Only sell if nothing great in shop
            shop_has_great = any(
                card.get("set") == "JOKER" and _score_shop_joker(
                    card, owned, hand_levels, strat, ante, joker_limit, deck_profile
                ) >= 6.0
                for card in shop.get("cards", [])
            )
            if not shop_has_great:
                candidates.append(ActionPlan(
                    steps=[SellJoker(diet_idx, reason="Diet Cola: sell for free shop reroll")],
                    net_value=1.5,  # moderate — free reroll has some value
                    description="sell Diet Cola",
                ))

        # 10. Reroll
        if (self._rerolls_this_shop < budget.reroll_cap
                and money - budget.reroll_cost >= budget.reserve):
            # Simple heuristic: reroll value = chance of finding something better
            best_shop_value = max(
                (c.net_value for c in candidates if "buy" in c.description.lower()),
                default=0.0,
            )
            # If nothing good in shop and we have slots, reroll is worth trying.
            # Scale by cheapness (cheap reroll = retry more freely) and by shop
            # slot bonus (more slots per roll = more EV per roll).
            base = 2.0 if slots_open and best_shop_value < 3.0 else 0.5
            reroll_value = base * (5 / budget.reroll_cost) * (1 + 0.4 * budget.shop_slot_bonus)
            opp_cost = _money_opportunity_cost(budget.reroll_cost, money, budget,
                                               budget.speculative_aggression,
                                               joker_count)
            net = reroll_value * budget.speculative_aggression - opp_cost
            if net > 0:
                candidates.append(ActionPlan(
                    steps=[Reroll(reason=f"reroll shop (${money}, reroll #{self._rerolls_this_shop + 1}, cost=${budget.reroll_cost})")],
                    net_value=net,
                    description="reroll shop",
                ))

        # 11. Proactive sells: decayed jokers (Popcorn, Ramen, dead Riff-Raff)
        for r in roster:
            if r.ev_delta <= 0.1 and r.key not in ("j_ceremonial", "j_invisible"):
                # This joker is essentially dead — sell proactively for cash
                effect = owned[r.index].get("value", {}).get("effect", "")
                parsed = parse_effect_value(effect) if effect else {}

                should_sell = False
                reason = ""

                if r.key == "j_popcorn" and (parsed.get("mult") or 0) <= 4:
                    should_sell = True
                    reason = f"Popcorn decayed to +{parsed.get('mult', 0)} mult"
                elif r.key == "j_ramen" and (parsed.get("xmult") or 1.0) < 1.0:
                    should_sell = True
                    reason = f"Ramen decayed below X1"
                elif r.key == "j_ice_cream" and (parsed.get("chips") or 0) <= 0:
                    should_sell = True
                    reason = "Ice Cream at 0 chips"
                elif r.key == "j_riff_raff" and joker_count >= joker_limit:
                    should_sell = True
                    reason = "Riff-Raff with no spawn slots"

                if should_sell:
                    candidates.append(ActionPlan(
                        steps=[SellJoker(r.index, reason=f"proactive sell: {reason}")],
                        net_value=r.sell_value + 1.0,  # cash + freeing slot
                        description=f"sell dead {r.label}",
                    ))

        # ── Rank and select ──
        if not candidates:
            return NextRound(reason="done shopping")

        best = max(candidates, key=lambda c: c.net_value)

        log.info(
            "[SHOP] %s (value=%.1f, budget=%s/$%d, aggression scoring=%.1f/spec=%.1f) | "
            "top candidates: %s",
            best.description, best.net_value,
            budget.phase, budget.spend_ceiling,
            budget.scoring_aggression, budget.speculative_aggression,
            ", ".join(f"{c.description}({c.net_value:.1f})" for c in
                      sorted(candidates, key=lambda c: -c.net_value)[:5]),
        )

        # Track state for multi-step plans
        if len(best.steps) > 1:
            self.pending_plan = best.steps[1:]
            # Record expected joker keys for sell actions so we can verify identity
            self._pending_sell_keys = {}
            for step in self.pending_plan:
                if isinstance(step, SellJoker) and step.index < len(owned):
                    self._pending_sell_keys[step.index] = joker_key(owned[step.index])

        action = best.steps[0]

        # Track reorder state to prevent cycles
        if isinstance(action, RearrangeJokers):
            self._last_order = action.order

        # Track reroll count
        if isinstance(action, Reroll):
            self._rerolls_this_shop += 1
        elif isinstance(action, SellJoker):
            if action.index < len(owned) and joker_key(owned[action.index]) == "j_diet_cola":
                self._rerolls_this_shop += 1  # Diet Cola sell triggers free shop refresh

        return action

    def _plan_still_valid(self, state: dict[str, Any]) -> bool:
        """Check if the pending plan's preconditions still hold."""
        if not self.pending_plan:
            return False

        next_action = self.pending_plan[0]

        # Validate sell actions: joker must still exist at that index
        # and be the same joker we intended to sell (indices shift after sells)
        if isinstance(next_action, SellJoker):
            owned = state.get("jokers", {}).get("cards", [])
            if next_action.index >= len(owned):
                return False
            expected_key = self._pending_sell_keys.get(next_action.index)
            if expected_key and joker_key(owned[next_action.index]) != expected_key:
                return False

        # Validate buy actions: must still be affordable
        if isinstance(next_action, BuyCard):
            money = state.get("money", 0)
            shop_cards = state.get("shop", {}).get("cards", [])
            if next_action.index >= len(shop_cards):
                return False
            cost = shop_cards[next_action.index].get("cost", {}).get("buy", 999)
            if cost > money:
                return False

        return True

    @staticmethod
    def _get_deck_profile(state: dict[str, Any]) -> DeckProfile:
        """Get or build+cache a DeckProfile."""
        cached = state.get("_deck_profile")
        if cached is not None:
            return cached
        from balatro_bot.domain.models.card import card_from_dict
        deck_cards = [card_from_dict(c) for c in state.get("cards", {}).get("cards", [])]
        hand_cards = [card_from_dict(c) for c in state.get("hand", {}).get("cards", [])]
        profile = DeckProfile.from_cards(deck_cards + hand_cards)
        state["_deck_profile"] = profile
        return profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_edition(card: dict) -> str | None:
    """Return the edition string for a card, or None."""
    mod = card.get("modifier")
    if not isinstance(mod, dict):
        return None
    return mod.get("edition")
