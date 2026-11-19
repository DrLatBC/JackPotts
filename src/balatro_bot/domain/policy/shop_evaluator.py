"""Unified shop evaluator — scores everything, plans a budget, ranks actions.

One system covers every shop action (sell, buy jokers / packs / vouchers /
consumables, Campfire feeding, Diet Cola sell, reorder, reroll, leave,
SellInvisible): it evaluates composite action plans and picks the best one.
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
from balatro_bot.domain.policy.consumable_policy import score_consumable, evaluate_hex
from balatro_bot.domain.policy.exploration import Choice, explorer
from balatro_bot.domain.policy.roster_order import describe_order, roster_order
from balatro_bot.domain.policy.run_view import run_view
from balatro_bot.domain.policy.shop_valuation import _KEY_TO_CATEGORY, joker_value
from balatro_bot.joker_effects.parsers import _ab_chips, _ab_mult, _ab_xmult
from balatro_bot.scaling import SELL_PROTECTED, check_anti_synergy
from balatro_bot.strategy import card_lean, compute_strategy

if TYPE_CHECKING:
    from typing import Any

    from balatro_bot.domain.policy.run_view import RunView

log = logging.getLogger("balatro_bot")

# ---------------------------------------------------------------------------
# Economy phases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Budget:
    """Economy plan for this shop visit.

    Two aggression dials separate concerns:
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


def score_roster(run: RunView) -> list[JokerScore]:
    """Compute live EV delta for each owned joker."""
    scores: list[JokerScore] = []

    owned = run.roster
    dagger_owned = any(joker_key(j) == "j_ceremonial" for j in owned)

    for i, j in enumerate(owned):
        key = joker_key(j)
        label = j.get("label", "?")
        sell_value = j.get("cost", {}).get("sell", 0)

        # EV delta: how much does removing this joker hurt scoring?
        ev = joker_value(j, run)

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
    kind: str           # which producer made it, e.g. "joker", "swap", "reroll"
    item: dict | None = None  # the shop card the plan acquires, if any


# ---------------------------------------------------------------------------
# Shop item scoring
# ---------------------------------------------------------------------------

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
    # lower than the raw heuristic suggests. Packs are the biggest ante 1-2
    # cash leak (1.8/ante vs 1.4 jokers/ante).
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
    run: RunView,
) -> list[tuple[int, str, float]]:
    """Return (index, label, feed_value) for consumables worth selling to Campfire.

    Returns empty list if Campfire is not owned.
    """
    owned_jokers = list(run.roster)
    if not any(joker_key(j) == "j_campfire" for j in owned_jokers):
        return []

    strat = compute_strategy(owned_jokers, run.hand_levels)
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
            hex_score = evaluate_hex(run)
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
    # free (cost_factor = 1 - 1.0 = 0) and the bot never banks cash.
    cost_factor = max(0.2, 1.0 - category_aggression)
    return lost_per_round * effective_rounds * cost_factor


# ---------------------------------------------------------------------------
# Shop view
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShopView:
    """Everything one shop decision is made from (see GLOSSARY.md)."""
    run: RunView
    budget: Budget
    roster: list[JokerScore]
    owned: list[dict]
    money: int
    ante: int
    round_num: int
    joker_count: int        # the API's count, which can lag len(owned)
    joker_limit: int
    slots_full: bool        # the API refused a buy for full slots this shop
    shop_cards: list[dict]
    pack_cards: list[dict]
    voucher_cards: list[dict]
    owned_vouchers: set[str]
    consumables: list[dict]
    consumable_count: int
    consumable_limit: int
    rerolls_this_shop: int
    invisible_first_seen: int | None
    invisible_selling_down: bool
    # Read only by the consumable producer: score_consumable takes the raw state.
    state: dict

    @property
    def actual_count(self) -> int:
        return max(self.joker_count, len(self.owned))

    @property
    def slots_open(self) -> bool:
        return self.actual_count < self.joker_limit and not self.slots_full


# ---------------------------------------------------------------------------
# Plan producers: shop view → candidate plans, one per kind of plan
# ---------------------------------------------------------------------------

def _invisible_plans(view: ShopView) -> list[ActionPlan]:
    """Sell down to Invisible Joker plus the best scorer, then sell Invisible to dupe it.

    Ranks dupe targets by live `ev_delta`, so any joker that currently carries
    the build is eligible.
    """
    roster, ante, round_num = view.roster, view.ante, view.round_num
    invisible_idx = next((s.index for s in roster if s.key == "j_invisible"), None)
    if invisible_idx is None or view.invisible_first_seen is None:
        return []
    if round_num - view.invisible_first_seen < 2 or len(roster) < 2:
        return []

    best = max(
        (s for s in roster if s.index != invisible_idx),
        key=lambda s: s.ev_delta,
        default=None,
    )
    if best is None:
        return []

    # Ante-scaled threshold on the same ~0-15 scale joker_value uses.
    threshold = 3.0 if ante <= 3 else 3.0 + (ante - 3) * 1.5
    if best.ev_delta < threshold:
        return []

    round_in_ante = ((round_num - 1) % 3) + 1
    boss_next = round_in_ante == 2
    if not view.invisible_selling_down and boss_next:
        return []

    if len(roster) == 2:
        return [ActionPlan(
            steps=[SellJoker(invisible_idx,
                             reason=f"Invisible: guaranteed dupe of {best.label} "
                                    f"(ev={best.ev_delta:.1f}, ante {ante})")],
            net_value=best.ev_delta * 10,  # duping is extremely high value
            description=f"Invisible dupe: {best.label}",
            kind="invisible_dupe",
        )]

    # Sell the roster member we'd miss least (lowest ev_delta, tiebreak by
    # highest sell_value so we also bank more cash on the way down).
    fodder = min(
        (s for s in roster if s.index not in (invisible_idx, best.index)),
        key=lambda s: (s.ev_delta, -s.sell_value),
    )
    remaining = len(roster) - 2
    return [ActionPlan(
        steps=[SellJoker(fodder.index,
                         reason=f"Invisible setup: sell {fodder.label} to isolate "
                                f"{best.label} ({remaining} more to go, "
                                f"target ev={best.ev_delta:.1f})")],
        net_value=best.ev_delta * 5,  # high priority — commit to sequence
        description=f"Invisible sell-down: selling {fodder.label}",
        kind="invisible_selldown",
    )]


def _leave_plans(view: ShopView) -> list[ActionPlan]:
    # A low floor: interest is protected by each purchase's opportunity cost,
    # so this only wins when nothing is worth buying. Speculation is cheap in
    # BUILD, which makes leaving more attractive than gambling on the shop.
    return [ActionPlan(
        steps=[NextRound(reason="done shopping")],
        net_value=0.5 * max(0.0, 1.0 - view.budget.speculative_aggression),
        description="leave shop",
        kind="leave",
    )]


def _joker_plans(view: ShopView) -> list[ActionPlan]:
    """Buy a joker into an open slot, or sell the weakest joker and buy it."""
    plans: list[ActionPlan] = []
    run, budget, money, owned = view.run, view.budget, view.money, view.owned
    owned_keys = {joker_key(j) for j in owned}
    riff_raff_owned = "j_riff_raff" in owned_keys
    free_slots = view.joker_limit - view.joker_count

    for i, card in enumerate(view.shop_cards):
        if card.get("set") != "JOKER":
            continue
        label = card.get("label", "?")
        cost = card.get("cost", {}).get("buy", 999)
        if cost > money:
            continue
        if check_anti_synergy(card.get("key", ""), owned_keys):
            continue

        shop_value = joker_value(card, run)
        # Riff-Raff needs free slots to spawn into.
        is_negative = _get_edition(card) == "NEGATIVE"
        if riff_raff_owned and free_slots <= 2 and not is_negative:
            shop_value *= 0.5
        if is_negative:
            shop_value = max(shop_value, 10.0)  # free slot is always high value

        # A Negative takes no slot once bought, but the game still requires
        # count < limit at buy time, so a full roster goes to the swap path.
        if view.slots_open or (is_negative and view.actual_count < view.joker_limit):
            opp_cost = _money_opportunity_cost(cost, money, budget,
                                               budget.scoring_aggression,
                                               view.joker_count)
            net = shop_value * budget.scoring_aggression - opp_cost
            if cost <= budget.spend_ceiling or is_negative or shop_value >= 8.0:
                plans.append(ActionPlan(
                    steps=[BuyCard(i, reason=f"buy {label} (${cost}, value={shop_value:.1f})")],
                    net_value=net,
                    description=f"buy {label}", kind="joker", item=card,
                ))
        elif view.roster:
            swap = _swap_plan(view, i, card, cost, shop_value, is_negative)
            if swap is not None:
                plans.append(swap)
    return plans


def _swap_plan(
    view: ShopView, i: int, card: dict, cost: int, shop_value: float, is_negative: bool,
) -> ActionPlan | None:
    """Sell the weakest sellable joker and buy shop card ``i`` (slots full).

    Only the single weakest joker that can be sold and afforded is weighed.
    """
    budget, owned, label = view.budget, view.owned, card.get("label", "?")
    for sc in sorted(view.roster, key=lambda r: r.ev_delta):
        # Selling a Negative takes its slot with it, so it never frees room
        # for the buy.
        if _get_edition(owned[sc.index]) in ("POLYCHROME", "NEGATIVE"):
            continue
        # Scaling jokers with accrued value and Gros Michel aren't sold for
        # a marginal upgrade.
        if joker_key(owned[sc.index]) in SELL_PROTECTED:
            continue
        effective_cost = cost - sc.sell_value
        if effective_cost > view.money:
            continue
        if is_negative:
            # Selling then buying a Negative is a net +1 joker: the sold
            # joker's slot comes back once the Negative lands.
            upgrade_delta = shop_value
            shop_value_post = shop_value
        else:
            # Price the buy against the post-sell roster so synergy with the
            # joker being sold doesn't inflate it.
            post_sell_owned = owned[:sc.index] + owned[sc.index + 1:]
            shop_value_post = joker_value(card, view.run.with_roster(post_sell_owned))
            upgrade_delta = shop_value_post - sc.ev_delta
        opp_cost = _money_opportunity_cost(max(0, effective_cost), view.money, budget,
                                           budget.scoring_aggression,
                                           view.joker_count)
        # 30% haircut keeps swaps conservative next to direct buys.
        net = upgrade_delta * budget.scoring_aggression * 0.7 - opp_cost
        if net <= 0:
            return None
        # Committed two-step plan: the buy runs as the next action instead of
        # being re-decided against rerolls and leaving, so a swap never ends
        # with the joker sold and nothing bought. Selling a joker leaves the
        # shop row untouched, so i still points at this card.
        buy_for = "Negative buy" if is_negative else f"{label} (post-sell value={shop_value_post:.1f})"
        return ActionPlan(
            steps=[
                SellJoker(sc.index, reason=f"sell {sc.label} (ev={sc.ev_delta:.1f}) for {buy_for}"),
                BuyCard(i, reason=f"buy {label} (${cost}, swap for {sc.label})"),
            ],
            net_value=net,
            description=f"sell {sc.label} → buy {label}", kind="swap", item=card,
        )
    return None


def _roster_order_plans(view: ShopView) -> list[ActionPlan]:
    """Put the roster in roster order. Blind select comes before the next
    reorder, so Ceremonial Dagger sits left of the joker it should eat, if any."""
    owned, roster = view.owned, view.roster
    fodder: JokerScore | None = None
    dagger_idx = next((i for i, j in enumerate(owned) if joker_key(j) == "j_ceremonial"), None)
    if dagger_idx is not None and roster:
        fodder_candidates = [
            r for r in roster
            if r.key != "j_ceremonial" and r.ev_delta < 1.0 and r.fodder_mult > 0
        ]
        if fodder_candidates:
            fodder = max(fodder_candidates, key=lambda r: (r.fodder_mult, -r.index))
            # A tied joker already right of Dagger stays the fodder. Swapping in
            # the other only moves this one to a lower index, where it wins the
            # tie next tick and the two trade places forever.
            fodder = next(
                (r for r in fodder_candidates
                 if r.index == dagger_idx + 1 and r.fodder_mult >= fodder.fodder_mult),
                fodder,
            )
    if not owned:
        return []
    new_order = roster_order(owned, fodder=fodder.index if fodder else None, blind_next=True)
    if new_order == list(range(len(owned))):
        return []
    if fodder is not None and fodder.index != dagger_idx + 1:
        return [ActionPlan(
            steps=[RearrangeJokers(
                order=new_order,
                reason=f"feed {fodder.label} to Dagger (+{fodder.fodder_mult:.0f} mult)")],
            net_value=fodder.fodder_mult * view.budget.scoring_aggression,
            description=f"feed {fodder.label} to Dagger",
            kind="dagger_feed",
        )]
    return [ActionPlan(
        steps=[RearrangeJokers(
            order=new_order, reason=f"scoring order: {describe_order(owned, new_order)}")],
        net_value=2.0,  # moderate priority — order matters but isn't urgent
        description="reorder for scoring",
        kind="reorder",
    )]


def _consumable_plans(view: ShopView) -> list[ActionPlan]:
    if view.consumable_count >= view.consumable_limit:
        return []
    plans: list[ActionPlan] = []
    budget = view.budget
    for i, card in enumerate(view.shop_cards):
        key = card.get("key", "")
        cost = card.get("cost", {}).get("buy", 999)
        label = card.get("label", "?")
        if card.get("set", "") not in ("TAROT", "PLANET", "SPECTRAL") and key not in PLANET_KEYS:
            continue
        if cost > view.money:
            continue
        value = score_consumable(key, view.state, view.run.strategy)
        if value <= 0:
            continue
        opp_cost = _money_opportunity_cost(cost, view.money, budget,
                                           budget.scoring_aggression,
                                           view.joker_count)
        net = value * budget.scoring_aggression - opp_cost
        if cost <= budget.spend_ceiling or value >= 4.0:
            plans.append(ActionPlan(
                steps=[BuyCard(i, reason=f"buy consumable: {label} (${cost}, value={value:.1f})")],
                net_value=net,
                description=f"buy {label}", kind="consumable", item=card,
            ))
    return plans


def _pack_plans(view: ShopView) -> list[ActionPlan]:
    plans: list[ActionPlan] = []
    budget, owned = view.budget, view.owned
    has_red_card = any(joker_key(j) == "j_red_card" for j in owned)
    for i, card in enumerate(view.pack_cards):
        label = card.get("label", "")
        cost = card.get("cost", {}).get("buy", 999)
        if cost > view.money:
            continue
        value = _score_pack(label, view.money, owned, view.ante, joker_limit=view.joker_limit)
        if value <= 0:
            continue
        if "Buffoon" in label and not has_red_card and view.joker_count >= view.joker_limit:
            continue
        # Celestial/Buffoon buy scoring power; Arcana/Spectral/Standard are
        # speculative (random cards with uncertain targeting value).
        if "Celestial" in label or "Buffoon" in label:
            pack_aggression = budget.scoring_aggression
        else:
            pack_aggression = budget.speculative_aggression
        opp_cost = _money_opportunity_cost(cost, view.money, budget, pack_aggression,
                                           view.joker_count)
        net = value * pack_aggression - opp_cost
        if cost <= budget.spend_ceiling or value >= 6.0:
            plans.append(ActionPlan(
                steps=[BuyPack(i, reason=f"buy pack: {label} (${cost}, value={value:.1f})")],
                net_value=net,
                description=f"buy {label}", kind="pack",
            ))
    return plans


def _voucher_plans(view: ShopView) -> list[ActionPlan]:
    plans: list[ActionPlan] = []
    budget = view.budget
    for i, card in enumerate(view.voucher_cards):
        key = card.get("key", "")
        cost = card.get("cost", {}).get("buy", 999)
        label = card.get("label", "?")
        value = _score_voucher(key, cost, view.money, budget, view.owned_vouchers)
        if value <= 0 or cost > view.money:
            continue
        opp_cost = _money_opportunity_cost(cost, view.money, budget,
                                           budget.speculative_aggression,
                                           view.joker_count)
        net = value * budget.speculative_aggression - opp_cost
        if cost <= budget.spend_ceiling or value >= 8.0:
            plans.append(ActionPlan(
                steps=[BuyVoucher(i, reason=f"buy voucher: {label} (${cost}, value={value:.1f})")],
                net_value=net,
                description=f"buy {label}", kind="voucher",
            ))
    return plans


def _campfire_plans(view: ShopView) -> list[ActionPlan]:
    return [
        ActionPlan(
            steps=[SellConsumable(idx, reason=f"Campfire: sell {label} (+X0.25 mult)")],
            net_value=feed_value * view.budget.scoring_aggression,
            description=f"Campfire feed: {label}",
            kind="campfire",
        )
        for idx, label, feed_value in _campfire_sell_candidates(view.consumables, view.run)
    ]


def _diet_cola_plans(view: ShopView) -> list[ActionPlan]:
    """Sell Diet Cola for its free reroll, unless the shop already has a strong joker."""
    diet_idx = next(
        (i for i, j in enumerate(view.owned) if joker_key(j) == "j_diet_cola"), None
    )
    if diet_idx is None:
        return []
    if any(card.get("set") == "JOKER" and joker_value(card, view.run) >= 6.0
           for card in view.shop_cards):
        return []
    return [ActionPlan(
        steps=[SellJoker(diet_idx, reason="Diet Cola: sell for free shop reroll")],
        net_value=1.5,  # moderate — free reroll has some value
        description="sell Diet Cola",
        kind="diet_cola",
    )]


_BUY_KINDS = frozenset({"joker", "swap", "consumable", "pack", "voucher"})


def _reroll_plans(view: ShopView, earlier: list[ActionPlan]) -> list[ActionPlan]:
    """Reroll, worth more when nothing produced so far is a good buy."""
    budget, money = view.budget, view.money
    if (view.rerolls_this_shop >= budget.reroll_cap
            or money - budget.reroll_cost < budget.reserve):
        return []
    best_shop_value = max(
        (p.net_value for p in earlier if p.kind in _BUY_KINDS), default=0.0,
    )
    # Cheaper rerolls retry more freely; more shop slots mean more per roll.
    base = 2.0 if view.slots_open and best_shop_value < 3.0 else 0.5
    reroll_value = base * (5 / budget.reroll_cost) * (1 + 0.4 * budget.shop_slot_bonus)
    opp_cost = _money_opportunity_cost(budget.reroll_cost, money, budget,
                                       budget.speculative_aggression,
                                       view.joker_count)
    net = reroll_value * budget.speculative_aggression - opp_cost
    if net <= 0:
        return []
    return [ActionPlan(
        steps=[Reroll(reason=f"reroll shop (${money}, reroll #{view.rerolls_this_shop + 1}, cost=${budget.reroll_cost})")],
        net_value=net,
        description="reroll shop",
        kind="reroll",
    )]


def _decay_sell_plans(view: ShopView) -> list[ActionPlan]:
    """Sell jokers that have decayed to nothing (Popcorn, Ramen, Ice Cream, stuck Riff-Raff)."""
    plans: list[ActionPlan] = []
    for r in view.roster:
        if r.ev_delta > 0.1 or r.key in ("j_ceremonial", "j_invisible"):
            continue
        j = view.owned[r.index]
        reason = ""
        # Fallbacks are the game's starting values (game.lua configs), so a
        # missing value never reads as decayed.
        if r.key == "j_popcorn" and (popcorn := _ab_mult(j, fallback=20)) <= 4:
            reason = f"Popcorn decayed to +{popcorn:g} mult"
        elif r.key == "j_ramen" and _ab_xmult(j, fallback=2.0) < 1.0:
            reason = "Ramen decayed below X1"
        elif r.key == "j_ice_cream" and _ab_chips(j, fallback=100) <= 0:
            reason = "Ice Cream at 0 chips"
        elif r.key == "j_riff_raff" and view.joker_count >= view.joker_limit:
            reason = "Riff-Raff with no spawn slots"
        if reason:
            plans.append(ActionPlan(
                steps=[SellJoker(r.index, reason=f"proactive sell: {reason}")],
                net_value=r.sell_value + 1.0,  # cash + freeing slot
                description=f"sell dead {r.label}",
                kind="decay_sell",
            ))
    return plans


# Every kind of plan, in the order produced. The order is behaviour: of two
# plans worth the same, the earlier one wins. A new shop behaviour is a new
# producer here.
_PRODUCERS = (
    _invisible_plans,
    _leave_plans,
    _joker_plans,
    _roster_order_plans,
    _consumable_plans,
    _pack_plans,
    _voucher_plans,
    _campfire_plans,
    _diet_cola_plans,
    _reroll_plans,
    _decay_sell_plans,
)
# Producers that also read the plans produced before them.
_REACTING = frozenset({_reroll_plans})


def _collect(view: ShopView) -> list[ActionPlan]:
    plans: list[ActionPlan] = []
    for produce in _PRODUCERS:
        plans.extend(produce(view, plans) if produce in _REACTING else produce(view))
    return plans


def _rank(plans: list[ActionPlan]) -> list[ActionPlan]:
    """Best first; a stable sort, so ties keep production order."""
    return sorted(plans, key=lambda p: -p.net_value)


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

class ShopEvaluator:
    """Unified shop decision engine."""

    def __init__(self) -> None:
        self.pending_plan: list[Action] | None = None
        self._pending_sell_keys: dict[int, str] = {}  # idx → expected joker key
        self._pending_buy_key: str | None = None  # shop card a pending BuyCard expects
        self._rerolls_this_shop: int = 0
        self._last_round: int = -1
        self.slots_full: bool = False  # set by bot.py on "slots are full" API error
        # What the last evaluate() weighed and chose; bot.py logs it per action.
        self.last_decision: dict | None = None
        # SellInvisible state
        self._invisible_first_seen: int | None = None
        self._invisible_selling_down: bool = False

    def continue_plan(self, state: dict[str, Any]) -> Action | None:
        """Next step of a committed multi-step plan, or None when there is none.

        Runs ahead of the other shop rules so nothing (e.g. a Judgement filling
        the slot a swap just freed) lands between a plan's steps.
        """
        self.last_decision = None
        self._reset_if_new_shop(state)
        if not self.pending_plan:
            return None
        if not self._plan_still_valid(state):
            log.info("[SHOP] pending plan invalidated, re-evaluating")
            self.pending_plan = None
            self._pending_sell_keys = {}
            return None
        action = self.pending_plan.pop(0)
        if not self.pending_plan:
            self.pending_plan = None
        self.last_decision = {"kind": "shop", "continuation": True}
        return action

    def _reset_if_new_shop(self, state: dict[str, Any]) -> None:
        round_num = state.get("round_num", 0)
        if round_num != self._last_round:
            self._rerolls_this_shop = 0
            self._last_round = round_num
            self.slots_full = False
            self.pending_plan = None
            self._pending_sell_keys = {}

    def ranked_plans(self, state: dict[str, Any]) -> list[ActionPlan]:
        """Every candidate plan for this tick, best first. Changes nothing."""
        return _rank(_collect(self._view(state)))

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        """Main entry point — called once per shop tick."""
        action = self.continue_plan(state)
        if action is not None:
            return action
        view = self._view(state)
        candidates = _collect(view)
        self._remember_invisible(view, candidates)
        return self._emit(view, candidates)

    def _view(self, state: dict[str, Any]) -> ShopView:
        money = state.get("money", 0)
        ante = state.get("ante_num", 1)
        round_num = state.get("round_num", 0)
        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])
        joker_limit = joker_info.get("limit", 5)
        joker_count = joker_info.get("count", 0)
        used_vouchers_raw = state.get("used_vouchers", {})
        owned_vouchers: set[str] = (
            set(used_vouchers_raw.keys()) if isinstance(used_vouchers_raw, dict) else set()
        )
        consumables_info = state.get("consumables", {})

        run = run_view(state)
        budget = compute_budget(money, ante, joker_count, owned_jokers=owned,
                                owned_vouchers=owned_vouchers)
        roster = score_roster(run)
        if joker_count != len(owned):
            log.warning("[SHOP] joker_count mismatch: API count=%d, len(cards)=%d, limit=%d",
                        joker_count, len(owned), joker_limit)
        # The per-shop counters reset when a new shop opens; evaluate() has
        # already reset them, ranked_plans() must not.
        same_shop = round_num == self._last_round
        return ShopView(
            run=run, budget=budget, roster=roster, owned=owned,
            money=money, ante=ante, round_num=round_num,
            joker_count=joker_count, joker_limit=joker_limit,
            slots_full=self.slots_full and same_shop,
            shop_cards=state.get("shop", {}).get("cards", []),
            pack_cards=state.get("packs", {}).get("cards", []),
            voucher_cards=state.get("vouchers", {}).get("cards", []),
            owned_vouchers=owned_vouchers,
            consumables=consumables_info.get("cards", []),
            consumable_count=consumables_info.get("count", 0),
            consumable_limit=consumables_info.get("limit", 2),
            rerolls_this_shop=self._rerolls_this_shop if same_shop else 0,
            invisible_first_seen=self._invisible_first_seen,
            invisible_selling_down=self._invisible_selling_down,
            state=state,
        )

    def _remember_invisible(self, view: ShopView, candidates: list[ActionPlan]) -> None:
        """Invisible Joker's hold timer and whether a sell-down is under way."""
        if not any(s.key == "j_invisible" for s in view.roster):
            self._invisible_first_seen, self._invisible_selling_down = None, False
        elif view.invisible_first_seen is None:
            self._invisible_first_seen, self._invisible_selling_down = view.round_num, False
        else:
            self._invisible_selling_down = any(
                p.kind == "invisible_selldown" for p in candidates
            )

    def _emit(self, view: ShopView, candidates: list[ActionPlan]) -> Action:
        """Pick the plan (greedy or explored), record it, and return its first step."""
        budget, owned = view.budget, view.owned
        greedy = max(candidates, key=lambda c: c.net_value)
        best, choice = _explore_plan(greedy, candidates)
        self.last_decision = _shop_decision_record(best, candidates, budget, view.roster)
        lean_set = best is not greedy and best.item is not None and explorer().set_lean(card_lean(best.item))
        explorer().tag(self.last_decision, choice)
        if choice.policy == "explore" and best is not greedy:
            log.info("[SHOP] exploring: %s instead of %s", best.description, greedy.description)
        if lean_set:
            self.last_decision["lean_set"] = True
            log.info("[SHOP] run lean set: %s", explorer().lean)

        log.info(
            "[SHOP] %s (value=%.1f, budget=%s/$%d, aggression scoring=%.1f/spec=%.1f) | "
            "top candidates: %s",
            best.description, best.net_value,
            budget.phase, budget.spend_ceiling,
            budget.scoring_aggression, budget.speculative_aggression,
            ", ".join(f"{c.description}({c.net_value:.1f})" for c in _rank(candidates)[:5]),
        )

        if len(best.steps) > 1:
            self.pending_plan = best.steps[1:]
            # Expected joker keys for pending sells, so a shifted index is caught.
            self._pending_sell_keys = {}
            self._pending_buy_key = best.item.get("key") if best.item else None
            for step in self.pending_plan:
                if isinstance(step, SellJoker) and step.index < len(owned):
                    self._pending_sell_keys[step.index] = joker_key(owned[step.index])

        action = best.steps[0]
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
            card = shop_cards[next_action.index]
            if self._pending_buy_key and card.get("key") != self._pending_buy_key:
                return False
            cost = card.get("cost", {}).get("buy", 999)
            if cost > money:
                return False
            jokers = state.get("jokers", {})
            if card.get("set") == "JOKER" and len(jokers.get("cards", [])) >= jokers.get("limit", 5):
                return False

        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DECISION_TOP_N = 8


def _explore_plan(greedy: ActionPlan, candidates: list[ActionPlan]) -> tuple[ActionPlan, Choice]:
    """The plan to act on: the greedy pick, or on an exploration roll a top-ranked runner-up.

    Joker reorders are scoring upkeep, not strategy, so they never enter the
    pool, and a tick whose greedy pick is a reorder doesn't explore. Once the
    run has a lean, runner-ups that point elsewhere are left out.
    """
    if isinstance(greedy.steps[0], RearrangeJokers):
        return greedy, Choice(0, "greedy", 1.0)
    ex = explorer()
    pool = [greedy] + sorted(
        (c for c in candidates
         if c is not greedy and not isinstance(c.steps[0], RearrangeJokers)
         and not (c.item is not None and ex.conflicts(card_lean(c.item)))),
        key=lambda c: -c.net_value,
    )
    choice = explorer().choose(len(pool))
    return pool[choice.idx], choice


def _shop_decision_record(
    best: ActionPlan,
    candidates: list[ActionPlan],
    budget: Budget,
    roster: list[JokerScore],
) -> dict:
    """The ranked plans, budget and roster scores behind one shop choice.

    The chosen plan is always candidates[0]; the rest are the runners-up by
    net_value, so a learner can see what the bot turned down. On an explored
    pick candidates[0] is the explored plan, not the highest-valued one.
    """
    ranked = [best] + sorted(
        (c for c in candidates if c is not best), key=lambda c: -c.net_value,
    )[:_DECISION_TOP_N - 1]
    return {
        "kind": "shop",
        "continuation": False,
        "chosen_idx": 0,
        "n_candidates": len(candidates),
        "candidates": [
            {
                "desc": c.description,
                "value": round(c.net_value, 3),
                "steps": [list(s.to_rpc()) for s in c.steps],
            }
            for c in ranked
        ],
        "budget": {
            "phase": budget.phase,
            "reserve": budget.reserve,
            "spend_ceiling": budget.spend_ceiling,
            "scoring_agg": budget.scoring_aggression,
            "speculative_agg": budget.speculative_aggression,
        },
        "roster_scores": [
            {"key": r.key, "ev": round(r.ev_delta, 3), "sell": r.sell_value,
             "effective": round(r.effective_value, 3)}
            for r in roster
        ],
    }


def _get_edition(card: dict) -> str | None:
    """Return the edition string for a card, or None."""
    mod = card.get("modifier")
    if not isinstance(mod, dict):
        return None
    return mod.get("edition")
