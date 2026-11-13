"""Blind-phase policy functions — pure decision logic for blind skip/select.

A stub: the bot always selects every blind. Planned blind-skip logic:
- Tag reward evaluation (Uncommon Tag = free joker, etc.)
- Boss blind difficulty assessment
- Red Card scaling (+3 mult per skip)
- Economy analysis (skip to save hands/discards for the boss)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


def choose_skip_for_tag(state: dict[str, Any]) -> bool:
    """Evaluate whether to skip the current blind for its tag reward.

    Returns True if the blind should be skipped, False otherwise.
    Always returns False (no-op stub).
    """
    return False
