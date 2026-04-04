"""Blind-phase policy functions — pure decision logic for blind skip/select.

Currently a stub — the bot always selects every blind.
This module provides the home for future blind-skip logic.
See CLAUDE.md TODO section for planned features:
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
    Currently always returns False (no-op stub).
    """
    return False
