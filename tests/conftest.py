"""Shared test fixtures — card factory helpers."""


def card(rank: str, suit: str, enhancement: str | None = None) -> dict:
    """Build a minimal card dict matching the balatrobot schema."""
    c: dict = {
        "id": 0,
        "key": f"{suit}_{rank}",
        "set": "DEFAULT",
        "label": f"{rank} of {suit}",
        "value": {"suit": suit, "rank": rank},
        "modifier": {},
        "state": {},
        "cost": {},
    }
    if enhancement:
        c["modifier"]["enhancement"] = enhancement
    return c


def stone_card() -> dict:
    return {
        "id": 0, "key": "stone", "set": "ENHANCED", "label": "Stone Card",
        "value": {}, "modifier": {"enhancement": "STONE"}, "state": {}, "cost": {},
    }


def wild_card(rank: str, suit: str) -> dict:
    c = card(rank, suit)
    c["modifier"]["enhancement"] = "WILD"
    return c


def joker(key: str, label: str = "") -> dict:
    """Build a minimal joker dict."""
    return {"key": key, "label": label or key, "set": "JOKER", "cost": {"sell": 3}}
