"""State adapter — converts raw balatrobot API dicts into typed snapshots."""

from __future__ import annotations

from balatro_bot.domain.models.snapshot import BlindSnapshot, RoundSnapshot, Snapshot


def adapt_state(state: dict) -> Snapshot:
    """Extract a typed Snapshot from a raw API state dict.

    Pure extraction — no computation, no boss-blind logic.
    """
    # Find the current blind
    current_blind = BlindSnapshot(key="", name="", score=0, status="")
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            current_blind = BlindSnapshot(
                key=b.get("key", ""),
                name=b.get("name", ""),
                score=b.get("score", 0),
                status="CURRENT",
                boss_disabled=state.get("_boss_disabled", False),
            )
            break

    rnd = state.get("round", {})
    round_snap = RoundSnapshot(
        chips=rnd.get("chips", 0),
        hands_left=rnd.get("hands_left", 0),
        discards_left=rnd.get("discards_left", 0),
        ancient_suit=rnd.get("ancient_suit"),
        most_played_poker_hand=rnd.get("most_played_poker_hand"),
    )

    return Snapshot(
        state_name=state.get("state", ""),
        seed=state.get("seed", ""),
        ante=state.get("ante_num", 1),
        round_num=state.get("round_num", 1),
        money=state.get("money", 0),
        joker_limit=state.get("jokers", {}).get("limit", 5),
        deck_count=state.get("cards", {}).get("count", 0),
        round=round_snap,
        current_blind=current_blind,
        hand_cards=state.get("hand", {}).get("cards", []),
        hand_levels=state.get("hands", {}),
        jokers=state.get("jokers", {}).get("cards", []),
        deck_cards=state.get("cards", {}).get("cards", []),
        consumables=state.get("consumables", {}).get("cards", []),
        shop_cards=state.get("shop", {}).get("cards", []),
        vouchers=state.get("vouchers", {}).get("cards", []),
        pack_cards=state.get("pack", {}).get("cards", []),
    )
