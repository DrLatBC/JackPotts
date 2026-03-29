"""Actions — what the bot can tell the game to do."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from typing import Any


@dataclass(frozen=True)
class PlayCards:
    card_indices: list[int]
    reason: str = ""
    hand_name: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "play", {"cards": self.card_indices}


@dataclass(frozen=True)
class DiscardCards:
    card_indices: list[int]
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "discard", {"cards": self.card_indices}


@dataclass(frozen=True)
class SelectBlind:
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "select", {}


@dataclass(frozen=True)
class SkipBlind:
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
    order: list[int]
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "rearrange", {"jokers": self.order}


@dataclass(frozen=True)
class RearrangeHand:
    order: list[int]
    reason: str = ""

    def to_rpc(self) -> tuple[str, dict]:
        return "rearrange", {"hand": self.order}


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
    | RearrangeJokers | RearrangeHand | UseConsumable | PackAction
)


class Rule(Protocol):
    """A rule returns an Action if it fires, or None to pass."""
    name: str
    def evaluate(self, state: dict[str, Any]) -> Action | None: ...
