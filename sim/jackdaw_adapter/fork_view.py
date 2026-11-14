"""Add the JackPotts balatrobot fork's gamestate fields to a Jackdaw view.

Jackdaw serves upstream balatrobot 1.5.2's view. The fork's serializer
(``src/lua/utils/gamestate.lua`` in DrLatBC/balatrobot) sends more, and the bot
reads it: card ``value.ability``/``value.rarity``, edition and enhancement
numbers under ``modifier``, ``round.ancient_suit``/``idol_card``/
``most_played_poker_hand`` and the ``counters`` block. ``augment`` computes them
from Jackdaw's snapshot (``Game.to_dict()``) the way the fork computes them from
the live card tables.

Not covered: ``earnings`` (the fork's money-attribution tracker; dashboard only).
"""

from __future__ import annotations

from typing import Any

from jackdaw.engine.data import load

CENTERS: dict[str, dict] = load("centers")

# gamestate area -> snapshot area
AREAS = {
    "jokers": "jokers",
    "consumables": "consumables",
    "cards": "deck",
    "hand": "hand",
    "shop": "shop_jokers",
    "vouchers": "shop_vouchers",
    "packs": "shop_booster",
    "pack": "pack_cards",
}

# Areas whose cards are in G.playing_cards.
PLAYING_CARD_AREAS = ("deck", "hand", "discard", "play")

SUIT_LETTER = {"Spades": "S", "Hearts": "H", "Clubs": "C", "Diamonds": "D"}
RANK_ENUM = {"10": "T", "Jack": "J", "Queen": "Q", "King": "K", "Ace": "A",
             **{str(n): str(n) for n in range(2, 10)}}

# Ability keys the fork copies when present (gamestate.lua extract_card_value).
COPIED_KEYS = ("driver_tally", "steel_tally", "stone_tally", "nine_tally", "loyalty_remaining",
               "yorick_discards", "caino_xmult", "invis_rounds", "to_do_poker_hand")


def _lua_number(v: Any) -> Any:
    """A number as the fork's JSON prints it (%.14g), e.g. 1.5999999999999999 -> 1.6."""
    if isinstance(v, float):
        f = float(f"{v:.14g}")
        return int(f) if f.is_integer() else f
    return v


def _nonzero(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0


def _card_id(card: dict) -> int | None:
    base = card.get("base")
    if card.get("center") == "m_stone" or not isinstance(base, dict):
        return None  # Card:get_id is a random negative for Stone cards
    return base.get("id")


def _per_frame_ability(card: dict, snap: dict) -> dict:
    """Values Card:update recomputes every frame (vanilla card.lua:4166-4248)."""
    center = card.get("center")
    ab = card.get("ability") or {}
    playing = [c for area in PLAYING_CARD_AREAS for c in snap["areas"].get(area, [])]
    if center == "j_drivers_license":
        return {"driver_tally": sum(1 for c in playing if c.get("center") != "c_base")}
    if center == "j_steel_joker":
        return {"steel_tally": sum(1 for c in playing if c.get("center") == "m_steel")}
    if center == "j_stone":
        return {"stone_tally": sum(1 for c in playing if c.get("center") == "m_stone")}
    if center == "j_cloud_9":
        return {"nine_tally": sum(1 for c in playing if _card_id(c) == 9)}
    if center == "j_throwback":
        return {"x_mult": 1 + (snap["game"].get("skips") or 0) * (ab.get("extra") or 0)}
    return {}


def fork_ability(card: dict, snap: dict) -> dict:
    """The fork's ``value.ability`` for one snapshot card."""
    ab = {**(card.get("ability") or {}), **_per_frame_ability(card, snap)}
    out: dict = {}
    extra = ab.get("extra")
    if isinstance(extra, dict):
        out.update({k: v for k, v in extra.items() if not isinstance(v, (dict, list))})
    elif extra is not None:
        out["extra"] = extra
    for k in ("t_mult", "t_chips", "mult", "x_mult"):
        if _nonzero(ab.get(k)):
            out[k] = ab[k]
    for k in COPIED_KEYS:
        if ab.get(k) is not None:
            out[k] = ab[k]
    return {k: _lua_number(v) for k, v in out.items()}


def fork_modifier_numbers(card: dict) -> dict:
    """Edition and enhancement numbers the fork adds to ``modifier``."""
    out: dict = {}
    edition = card.get("edition")
    if isinstance(edition, dict):
        if _nonzero(edition.get("mult")):
            out["edition_mult"] = edition["mult"]
        if _nonzero(edition.get("chips")):
            out["edition_chips"] = edition["chips"]
        # Card:get_edition returns nothing for a debuffed card (Steamodded card.lua:1254).
        if not card.get("debuff") and _nonzero(edition.get("x_mult")):
            out["edition_x_mult"] = edition["x_mult"]
    effect = CENTERS.get(card.get("center"), {}).get("effect")
    x_mult = (card.get("ability") or {}).get("x_mult")
    if effect and effect != "Base" and isinstance(x_mult, (int, float)) and x_mult != 1:
        out["enhancement_x_mult"] = x_mult
    return {k: _lua_number(v) for k, v in out.items()}


def fork_round(snap: dict) -> dict:
    cr = snap.get("current_round") or {}
    out: dict = {}
    if cr.get("most_played_poker_hand"):
        out["most_played_poker_hand"] = cr["most_played_poker_hand"]
    ancient = (cr.get("ancient_card") or {}).get("suit")
    if ancient:
        out["ancient_suit"] = SUIT_LETTER.get(ancient, ancient)
    idol = cr.get("idol_card") or {}
    if idol.get("rank") and idol.get("suit"):
        out["idol_card"] = {"rank": RANK_ENUM.get(idol["rank"], idol["rank"]),
                            "suit": SUIT_LETTER.get(idol["suit"], idol["suit"])}
    return out


def fork_counters(snap: dict) -> dict:
    usage = snap.get("consumeable_usage") or {}
    by_key = usage.get("by_key") or {}
    return {
        "tarots_used": (usage.get("totals") or {}).get("tarot", 0),
        "planets_used_unique": sum(1 for k in by_key if CENTERS.get(k, {}).get("set") == "Planet"),
        "starting_deck_size": snap["game"].get("starting_deck_size"),
        "playing_cards": sum(len(snap["areas"].get(a, [])) for a in PLAYING_CARD_AREAS),
        "probabilities_normal": snap["game"].get("probabilities_normal", 1),
    }


def augment(view: Any, snap: dict | None) -> Any:
    """Add the fork-only fields to a gamestate view, in place."""
    if not isinstance(view, dict) or snap is None or "state" not in view:
        return view
    for view_area, snap_area in AREAS.items():
        area = view.get(view_area)
        if not isinstance(area, dict):
            continue
        by_id = {c.get("sort_id"): c for c in snap["areas"].get(snap_area, [])}
        for vc in area.get("cards") or []:
            sc = by_id.get(vc.get("id"))
            if sc is None:
                continue
            value = vc["value"] if isinstance(vc.get("value"), dict) else {}
            vc["value"] = value
            ability = fork_ability(sc, snap)
            if ability:
                value["ability"] = ability
            rarity = CENTERS.get(sc.get("center"), {}).get("rarity")
            if rarity is not None:
                value["rarity"] = rarity
            numbers = fork_modifier_numbers(sc)
            if numbers:
                modifier = vc["modifier"] if isinstance(vc.get("modifier"), dict) else {}
                modifier.update(numbers)
                vc["modifier"] = modifier
    rnd = view.get("round")
    if isinstance(rnd, dict):
        rnd.update(fork_round(snap))
    view["counters"] = fork_counters(snap)
    return view
