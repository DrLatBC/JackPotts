from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.actions import CashOut, Action

if TYPE_CHECKING:
    from typing import Any


class AlwaysCashOut:
    name = "always_cash_out"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return CashOut(reason="cash out")
