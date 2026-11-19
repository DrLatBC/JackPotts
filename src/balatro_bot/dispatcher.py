"""Sends the bot's actions to the game and recovers from what the game refuses."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from balatrobot import APIError

from balatro_bot.domain.scoring.play_order import play_plan

if TYPE_CHECKING:
    from balatro_bot.engine import RuleEngine
    from balatro_bot.infrastructure.client import BalatroClient

log = logging.getLogger("balatro_bot")

PACK_STATES = frozenset({
    "TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK",
    "STANDARD_PACK", "BUFFOON_PACK", "SMODS_BOOSTER_OPENED",
})
MAX_ERRORS = 5
WEDGE_ERRORS = 3
WEDGE_ABORT = 2


@dataclass
class Sent:
    """What came of one action. ``outcome`` is "acted", "refused" (``error`` set) or
    "timed_out"; ``state`` is the game state to carry on from. ``recording_at`` is the
    action's place in the client's replay recording, when one is kept."""

    outcome: str
    state: dict[str, Any]
    error: APIError | None = None
    recording_at: int | None = None


def _carry(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    # The game never reports a disabled boss, so the flag set when Luchador is sold
    # lives only in the bot's state dicts and must pass to every state that replaces one.
    if old.get("_boss_disabled", False):
        new["_boss_disabled"] = True
    return new


class Dispatcher:
    """One game's connection: polls, plays in score order, and retries or skips when
    the game refuses an action."""

    def __init__(
        self, client: BalatroClient, engine: RuleEngine, *,
        poll_interval: float = 0.2, stream_delay: float = 0.0,
    ) -> None:
        self.client = client
        self.engine = engine
        self.poll_interval = poll_interval
        self.stream_delay = stream_delay
        self.consecutive_errors = 0
        self.pack_stuck_count = 0

    def poll(self, state: dict[str, Any]) -> dict[str, Any]:
        return _carry(state, self.client.call("gamestate"))

    def wait(self, state: dict[str, Any]) -> dict[str, Any]:
        time.sleep(self.poll_interval)
        return self.poll(state)

    def prepare(
        self, method: str, params: dict | None, state: dict[str, Any],
    ) -> dict[str, Any]:
        """Balatro scores played cards left to right by hand position, not by click
        order, so a play out of hand order first rearranges the hand: the played cards
        swap among their own slots and ``params`` then names those slots. Returns the
        state to act on."""
        if method != "play" or not params or "cards" not in params:
            return state
        rearrange, played = play_plan(params["cards"], len(state.get("hand", {}).get("cards", [])))
        if rearrange is None:
            return state
        try:
            self.client.call("rearrange", {"hand": rearrange})
            params["cards"] = played
            state = self.poll(state)
        except Exception:
            log.warning("rearrange failed, playing in original order")
        return state

    def send(
        self, game_state: str, method: str, params: dict | None, state: dict[str, Any],
    ) -> Sent:
        """Send one action prepared by ``prepare``. Raises when the game stops
        answering or its pack state is wedged beyond a forced skip."""
        if self.stream_delay > 0 and method in ("play", "discard") and params and "cards" in params:
            for card_idx in params["cards"]:
                try:
                    self.client.call("highlight", {"card": card_idx})
                except Exception:
                    pass
                time.sleep(0.5)
        try:
            after = _carry(state, self.client.call(method, params))
        except httpx.TimeoutException:
            return Sent("timed_out", self._after_timeout(method, params, state))
        except APIError as e:
            return Sent("refused", self._after_refusal(game_state, method, params, state, e), error=e)
        self.consecutive_errors = 0
        recording = self.client.recording
        sent = Sent("acted", after, recording_at=None if recording is None else len(recording) - 1)
        if self.stream_delay > 0:
            time.sleep(self.stream_delay)
        return sent

    def _after_timeout(self, method: str, params: dict | None, state: dict[str, Any]) -> dict[str, Any]:
        log.warning("Timeout on %s(%s) — re-polling gamestate", method, params)
        try:
            state = self.poll(state)
        except httpx.TimeoutException:
            log.error("Double timeout — server unresponsive, aborting")
            raise
        if state.get("state") == "GAME_OVER":
            return state
        pack_cards = state.get("pack", {}).get("cards", [])
        log.warning(
            "  post-timeout state=%s pack=%s money=%s ante=%s round=%s",
            state.get("state", "?"), [c.get("label", "?") for c in pack_cards] or "none",
            state.get("money"), state.get("ante_num"), state.get("round_num"),
        )
        for i, c in enumerate(pack_cards):
            log.warning("  pack[%d]: label=%s key=%s set=%s", i, c.get("label"), c.get("key"), c.get("set"))
        return state

    def _after_refusal(
        self, game_state: str, method: str, params: dict | None, state: dict[str, Any], e: APIError,
    ) -> dict[str, Any]:
        self.consecutive_errors += 1
        log.warning("API error: %s (%s) — retry %d", e.message, e.name, self.consecutive_errors)
        self.engine.refused(game_state, method, params, e)

        # A pack pick wedged on the mod side ("already in progress") does not clear by
        # retrying; a skip that also fails means only a restart will.
        if method == "pack" and e.name == "NOT_ALLOWED" and self.consecutive_errors >= WEDGE_ERRORS:
            try:
                skipped = _carry(state, self.client.call("pack", {"skip": True}))
                self.consecutive_errors = 0
                self.pack_stuck_count = 0
                log.warning("Pack pick wedged — forced skip succeeded")
                return skipped
            except APIError as skip_err:
                self.pack_stuck_count += 1
                log.error(
                    "Pack force-skip also failed (%s: %s) — stuck %d/%d",
                    skip_err.name, skip_err.message, self.pack_stuck_count, WEDGE_ABORT,
                )
                if self.pack_stuck_count >= WEDGE_ABORT:
                    log.error("Pack state wedged, aborting game for supervisor restart")
                    raise RuntimeError(f"pack state wedged: {skip_err.name}") from skip_err

        if self.consecutive_errors >= MAX_ERRORS:
            log.error("Too many consecutive errors, forcing skip")
            if game_state in PACK_STATES:
                try:
                    self.client.call("pack", {"skip": True})
                except APIError:
                    pass
            self.consecutive_errors = 0
            return self.poll(state)
        return self.wait(state)
