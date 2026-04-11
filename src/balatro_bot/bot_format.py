"""Card and deck formatting utilities for the game loop."""

from __future__ import annotations

from collections import defaultdict

# Card formatting constants
SUIT_SYM = {"H": "\u2665", "D": "\u2666", "C": "\u2663", "S": "\u2660"}
RANK_SYM = {
    "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    "7": "7", "8": "8", "9": "9", "T": "10",
    "J": "J", "Q": "Q", "K": "K", "A": "A",
}


def fmt_card(c: dict) -> str:
    """Format a card dict as a compact label like '10\u2665' or 'A\u2660'."""
    val = c.get("value", {})
    return RANK_SYM.get(val.get("rank", ""), "?") + SUIT_SYM.get(val.get("suit", ""), "?")


def format_card_detail(method: str, params: dict | None, state: dict) -> str:
    """Build card detail string for action logging."""
    if method in ("play", "discard") and "cards" in (params or {}):
        hand_cards = state.get("hand", {}).get("cards", [])
        indices = params["cards"]
        labels = [fmt_card(hand_cards[i]) for i in indices if i < len(hand_cards)]
        return f" [{', '.join(labels)}]"
    if method == "use" and params and "cards" in params:
        hand_cards = state.get("hand", {}).get("cards", [])
        indices = params["cards"]
        labels = [fmt_card(hand_cards[i]) for i in indices if i < len(hand_cards)]
        return f" targets:[{', '.join(labels)}]"
    if method == "pack" and params and "targets" in params:
        hand_cards = state.get("hand", {}).get("cards", [])
        indices = params["targets"]
        labels = [fmt_card(hand_cards[i]) for i in indices if i < len(hand_cards)]
        return f" targets:[{', '.join(labels)}]"
    return ""


def format_deck_snapshot(deck_cards: list) -> str:
    """Format a compact deck snapshot for logging at ante transitions."""
    ENHANCEMENT_ABBR = {
        "GLASS": "GL", "STEEL": "ST", "WILD": "WL",
        "BONUS": "BN", "GOLD": "GD", "LUCKY": "LK", "MULT": "MU",
    }
    SEAL_ABBR = {
        "Red Seal": "RS", "Gold Seal": "GS",
        "Blue Seal": "BS", "Purple Seal": "PS",
    }
    SUIT_SYM_DECK = {"S": "\u2660", "H": "\u2665", "D": "\u2666", "C": "\u2663"}
    RANK_ORDER = ["A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2"]

    by_rank: dict[str, list] = defaultdict(list)
    stone_count = 0

    for card in deck_cards:
        mod = card.get("modifier", {})
        if not isinstance(mod, dict):
            mod = {}
        enh = mod.get("enhancement", "")
        if enh == "STONE":
            stone_count += 1
            continue
        rank = card.get("value", {}).get("rank")
        suit = card.get("value", {}).get("suit")
        if not rank or not suit:
            continue
        seal = mod.get("seal", "")
        by_rank[rank].append((suit, enh, seal))

    parts = []
    for rank in RANK_ORDER:
        if rank not in by_rank:
            continue
        inner = rank
        for suit, enh, seal in sorted(by_rank[rank], key=lambda x: x[0]):
            sym = SUIT_SYM_DECK.get(suit, suit)
            suffix = ENHANCEMENT_ABBR.get(enh, "") + SEAL_ABBR.get(seal, "")
            inner += sym + suffix
        parts.append(f"[{inner}]")

    if stone_count:
        parts.append(f"[STN\u00d7{stone_count}]")

    total = sum(len(v) for v in by_rank.values()) + stone_count
    return f"({total}): " + " ".join(parts)
