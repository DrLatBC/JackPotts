"""Typed joker model — replaces raw dict access for jokers.

The factory ``joker_from_dict`` handles the balatrobot API quirk of
returning ``[]`` instead of ``{}`` for empty nested objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from balatro_bot.domain.models.card import CardModifier, CardState, _safe_dict


@dataclass(frozen=True)
class JokerAbility:
    chips: float | None = None
    mult: float | None = None
    t_mult: float | None = None
    t_chips: float | None = None
    Xmult: float | None = None
    x_mult: float | None = None
    s_mult: float | None = None
    extra: float | None = None
    size: int | None = None
    d_remaining: int | None = None
    loyalty_remaining: int | None = None
    min: float | None = None
    max: float | None = None
    hand_add: float | None = None
    chip_mod: float | None = None
    dollars: float | None = None
    driver_tally: int | None = None
    odds: int | None = None
    poker_hand: str | None = None
    to_do_poker_hand: str | None = None


@dataclass(frozen=True)
class JokerValue:
    effect: str = ""
    rarity: int | str | None = None
    ability: JokerAbility = field(default_factory=JokerAbility)


@dataclass(frozen=True)
class Joker:
    key: str = ""
    label: str = ""
    set_: str = ""
    value: JokerValue = field(default_factory=JokerValue)
    modifier: CardModifier = field(default_factory=CardModifier)
    state: CardState = field(default_factory=CardState)
    cost: dict = field(default_factory=dict)


def joker_from_dict(d: dict | Joker) -> Joker:
    """Convert a raw API joker dict into a typed Joker.  Pass-through if already a Joker."""
    if isinstance(d, Joker):
        return d

    val = _safe_dict(d.get("value", {}))
    mod = _safe_dict(d.get("modifier", {}))
    st = _safe_dict(d.get("state", {}))
    ab = _safe_dict(val.get("ability", {}))

    return Joker(
        key=d.get("key", ""),
        label=d.get("label", ""),
        set_=d.get("set", ""),
        value=JokerValue(
            effect=val.get("effect", "") or "",
            rarity=val.get("rarity"),
            ability=JokerAbility(
                chips=ab.get("chips"),
                mult=ab.get("mult"),
                t_mult=ab.get("t_mult"),
                t_chips=ab.get("t_chips"),
                Xmult=ab.get("Xmult"),
                x_mult=ab.get("x_mult"),
                s_mult=ab.get("s_mult"),
                extra=ab.get("extra"),
                size=ab.get("size"),
                d_remaining=ab.get("d_remaining"),
                loyalty_remaining=ab.get("loyalty_remaining"),
                min=ab.get("min"),
                max=ab.get("max"),
                hand_add=ab.get("hand_add"),
                chip_mod=ab.get("chip_mod"),
                dollars=ab.get("dollars"),
                driver_tally=ab.get("driver_tally"),
                odds=ab.get("odds"),
                poker_hand=ab.get("poker_hand"),
                to_do_poker_hand=ab.get("to_do_poker_hand"),
            ),
        ),
        modifier=CardModifier(
            enhancement=mod.get("enhancement"),
            edition=mod.get("edition"),
            seal=mod.get("seal"),
            edition_chips=mod.get("edition_chips", 0) or 0,
            edition_mult=mod.get("edition_mult", 0) or 0,
            edition_x_mult=mod.get("edition_x_mult", 0) or 0,
            enhancement_x_mult=mod.get("enhancement_x_mult", 0) or 0,
        ),
        state=CardState(debuff=st.get("debuff", False) is True),
        cost=d.get("cost") or {},
    )
