"""Main bot loop — connects to balatrobot and runs the decision engine."""

from __future__ import annotations

import logging
import logging.handlers
import random
import re
import string
import time
from dataclasses import dataclass, field

import httpx
from balatrobot import APIError, BalatroClient

from balatro_bot.bot_format import fmt_card, format_card_detail
from balatro_bot.engine import RuleEngine

log = logging.getLogger("balatro_bot")
_stream_log = logging.getLogger("balatro_stream")
_scoring_log = logging.getLogger("balatro_scoring")


@dataclass
class GameLoopState:
    """Mutable state tracked across the game loop in run_bot()."""

    actions_taken: int = 0
    consecutive_errors: int = 0
    total_hands_played: int = 0
    total_discards_used: int = 0
    hands_at_blind_start: int = 0
    discards_at_blind_start: int = 0
    last_logged_hand: tuple | None = None
    last_logged_shop: tuple | None = None
    last_logged_blind: tuple | None = None
    prev_joker_keys: set = field(default_factory=set)
    hand_context_logged: bool = False
    prev_hand_labels: list = field(default_factory=list)
    last_ante: int | None = None
    last_round: int | None = None
    current_blind_name: str | None = None
    current_blind_target: int | str | None = None
    max_chips_this_blind: int = 0
    won_logged: bool = False
    actually_won: bool = False


class WinCaptureHandler(logging.handlers.MemoryHandler):
    """Buffers all log records; on flush_win() writes them to wins.txt."""

    def __init__(self, fmt: logging.Formatter, wins_file: str = "wins.txt") -> None:
        super().__init__(capacity=100_000, flushLevel=logging.CRITICAL + 1, target=None)
        self.fmt = fmt
        self.wins_file = wins_file

    def shouldFlush(self, record: logging.LogRecord) -> bool:
        return False

    def flush_win(self, seed: str) -> None:
        with open(self.wins_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"VICTORY — seed {seed}\n")
            f.write(f"{'='*60}\n")
            for record in self.buffer:
                f.write(self.fmt.format(record) + "\n")
        self.buffer.clear()

    def reset(self) -> None:
        self.buffer.clear()


_win_handler: WinCaptureHandler | None = None


def setup_logging(
    verbose: bool = False,
    log_file: str = "growing.txt",
    wins_file: str = "wins.txt",
    scoring_file: str | None = None,
    stream_file: str | None = None,
) -> None:
    global _win_handler
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    _win_handler = WinCaptureHandler(fmt, wins_file=wins_file)
    _win_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if not stream_file:
        root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(_win_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)

    if scoring_file:
        scoring_log = logging.getLogger("balatro_scoring")
        scoring_log.setLevel(logging.INFO)
        scoring_log.propagate = False
        sh = logging.FileHandler(scoring_file, mode="a", encoding="utf-8")
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        scoring_log.addHandler(sh)

    if stream_file:
        stream_logger = logging.getLogger("balatro_stream")
        stream_logger.setLevel(logging.INFO)
        stream_logger.propagate = False
        stream_fmt = logging.Formatter("%(message)s")
        sfh = logging.FileHandler(stream_file, mode="a", encoding="utf-8")
        sfh.setFormatter(stream_fmt)
        stream_logger.addHandler(sfh)
        # Also print to console for OBS window capture
        sc = logging.StreamHandler()
        sc.setFormatter(stream_fmt)
        stream_logger.addHandler(sc)


def wait_for_server(client: BalatroClient, timeout: float = 30.0) -> None:
    """Block until the balatrobot API responds."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            client.call("health")
            log.info("Connected to balatrobot API")
            return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"balatrobot API not reachable after {timeout}s")


# ---------------------------------------------------------------------------
# Core loop helpers (state queries, victory detection)
# ---------------------------------------------------------------------------

def find_current_blind(state: dict) -> tuple[str, int | str] | None:
    """Find the current/selected blind from game state. Returns (name, score) or None."""
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            return b.get("name", "?"), b.get("score", "?")
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "SELECT":
            return b.get("name", "?"), b.get("score", "?")
    return None


def _check_victory(
    gs: GameLoopState, state: dict,
    pre_play_chips: int = 0, expected_hand_score: int = 0,
) -> bool:
    """Detect ante 9+ victory. Returns True if newly detected."""
    from balatro_bot.bot_logging import log_blind_result

    ante = state.get("ante_num", 0)
    if ante < 9 or gs.actually_won:
        return False

    gs.actually_won = True

    if pre_play_chips or expected_hand_score:
        # Timeout victory — estimate final chips from pre-play + expected score
        if gs.current_blind_name:
            target = gs.current_blind_target if isinstance(gs.current_blind_target, int) else 0
            final_chips = pre_play_chips + expected_hand_score
            gs.max_chips_this_blind = max(gs.max_chips_this_blind, final_chips)
            log.info(
                "VICTORY at ante %s round %s (seed=%s) — scored %s / needed %s — WON | %d hands, %d discards",
                ante, state.get("round_num", "?"), state.get("seed", "?"),
                f"{final_chips:,}", f"{target:,}",
                gs.total_hands_played - gs.hands_at_blind_start,
                gs.total_discards_used - gs.discards_at_blind_start,
            )
        else:
            log.info(
                "VICTORY at ante %s round %s (seed=%s)",
                ante, state.get("round_num", "?"), state.get("seed", "?"),
            )
    else:
        # Normal victory — log to both loggers
        log.info(
            "VICTORY at ante %s round %s (seed=%s) — entering endless mode",
            ante, state.get("round_num", "?"), state.get("seed", "?"),
        )
        _stream_log.info("")
        _stream_log.info("*** VICTORY! ***")
        log_blind_result(gs, "WON")

    gs.current_blind_name = None
    return True


# ---------------------------------------------------------------------------
# Main game loop
# ---------------------------------------------------------------------------

def run_bot(
    client: BalatroClient,
    engine: RuleEngine,
    *,
    start_game: bool = False,
    deck: str = "RED",
    stake: str = "WHITE",
    seed: str | None = None,
    poll_interval: float = 0.2,
    stream_delay: float = 0.0,
) -> bool:
    """Main bot loop. Returns True if the game was won."""
    from balatro_bot.bot_logging import (
        build_play_snapshot, detect_joker_changes, log_action,
        log_ante_transition, log_blind_transition, log_game_over,
        log_hand_state, log_played_hand, log_shop_state,
    )

    if start_game:
        try:
            client.call("menu")
        except APIError:
            pass
        if seed is None:
            seed = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        for _attempt in range(3):
            try:
                state = client.call("start", {"deck": deck, "stake": stake, "seed": seed})
                break
            except APIError as e:
                log.warning("start() failed (attempt %d): %s — retrying", _attempt + 1, e.message)
                time.sleep(1)
                try:
                    client.call("menu")
                except APIError:
                    pass
                time.sleep(1)
        else:
            raise RuntimeError("Failed to start game after 3 attempts")
        log.info("Started new game: deck=%s stake=%s seed=%s", deck, stake, state.get("seed"))
        _stream_log.info("New game — Seed: %s | Deck: %s", state.get("seed"), deck)
    else:
        state = client.call("gamestate")
        log.info("Joined existing game: state=%s", state.get("state"))

    gs = GameLoopState()

    while state.get("state") != "GAME_OVER":
        game_state = state.get("state", "")

        # Detect real win: bot advanced past ante 8 (beat the boss)
        # Don't trust state.won — Hieroglyph voucher inflates ante counter
        cur_ante = state.get("ante_num", 0)
        _check_victory(gs, state)

        # Log when state.won fires (may be premature due to Hieroglyph)
        if state.get("won") and not gs.won_logged:
            if not gs.actually_won:
                log.info(
                    "state.won=true at ante %s (Hieroglyph?) — not a real win until ante 9+",
                    state.get("ante_num", "?"),
                )
            gs.won_logged = True

        log_ante_transition(gs, state)
        # Track highest chips seen during this blind (capture before round resets)
        cur_chips = state.get("round", {}).get("chips", 0)
        if cur_chips > gs.max_chips_this_blind:
            gs.max_chips_this_blind = cur_chips
        gs.last_round = state.get("round_num")

        if game_state in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "SPLASH", "TUTORIAL"):
            gs.last_logged_hand = None
            time.sleep(poll_interval)
            state = client.call("gamestate")
            continue

        if game_state != "SELECTING_HAND":
            gs.last_logged_hand = None
            gs.prev_hand_labels = []

        if game_state != "SHOP":
            gs.last_logged_shop = None

        if game_state == "SHOP":
            log_shop_state(gs, state)

        if game_state == "BLIND_SELECT":
            log_blind_transition(gs, state)

        if game_state != "BLIND_SELECT":
            gs.last_logged_blind = None

        if game_state == "SELECTING_HAND":
            log_hand_state(gs, state)

        # Refresh state before deciding — previous action response may have
        # stale money, joker counters, or debuff flags.
        if game_state == "SELECTING_HAND":
            _boss_disabled_before = state.get("_boss_disabled", False)
            state = client.call("gamestate")
            if _boss_disabled_before:
                state["_boss_disabled"] = True

        action = engine.decide(state)
        if action is None:
            log.debug("No rule matched for state=%s, polling...", game_state)
            time.sleep(poll_interval)
            state = client.call("gamestate")
            continue

        method, params = action.to_rpc()

        # Rearrange hand cards so scoring order matches the bot's intended play order.
        # Balatro scores left-to-right by hand position, not by click order.
        if method == "play" and params and "cards" in params:
            play_indices = params["cards"]
            if play_indices != sorted(play_indices):
                hand_size = len(state.get("hand", {}).get("cards", []))
                non_play = [i for i in range(hand_size) if i not in set(play_indices)]
                new_order = play_indices + non_play  # played cards first, rest after
                try:
                    client.call("rearrange", {"hand": new_order})
                    # Remap: played cards are now at positions 0..len-1
                    params["cards"] = list(range(len(play_indices)))
                    # Refresh state so card labels reflect new order
                    state = client.call("gamestate")
                except Exception:
                    log.warning("rearrange failed, playing in original order")

        card_detail = format_card_detail(method, params, state)
        log_action(gs, game_state, method, params, action, card_detail, state)

        # Stream mode: highlight cards one at a time before playing/discarding
        if stream_delay > 0 and method in ("play", "discard") and params and "cards" in params:
            for card_idx in params["cards"]:
                try:
                    client.call("highlight", {"card": card_idx})
                except Exception:
                    pass
                time.sleep(0.5)

        pre_play_chips = 0
        play_snapshot = None
        if method == "play":
            pre_play_chips = state.get("round", {}).get("chips", 0)
            _scoring_log.info("  PRE_PLAY chips_in_round=%d", pre_play_chips)
            play_snapshot = build_play_snapshot(state, params, action)

        # Capture expected hand score for chip accounting on timeout
        expected_hand_score = 0
        if method == "play":
            m = re.search(r"for (\d+)", getattr(action, "reason", ""))
            if m:
                expected_hand_score = int(m.group(1))

        # Shorter timeout on ante 8 plays — the win screen hangs the mod
        saved_timeout = None
        if method == "play" and cur_ante == 8:
            saved_timeout = client.timeout
            client.timeout = 5.0

        try:
            _boss_disabled_before = state.get("_boss_disabled", False)
            state = client.call(method, params)
            # Preserve _boss_disabled across state replacement — the API
            # never reports blind.disabled, so this side-channel flag
            # (set when Luchador is sold) would otherwise be lost.
            if _boss_disabled_before:
                state["_boss_disabled"] = True
            gs.actions_taken += 1
            if method == "play":
                gs.total_hands_played += 1
                post_chips = state.get("round", {}).get("chips", 0)
                _scoring_log.info("  POST_PLAY chips_in_round=%d, delta=%d", post_chips, post_chips - pre_play_chips)
                # The Hook discards 2 held cards BEFORE scoring (before
                # cards are replenished).  The pre-play snapshot is used
                # as-is; joker corrections (Ramen, Yorick, Green Joker)
                # are applied in joker_effects/complex.py.
                log_played_hand(play_snapshot, pre_play_chips, state)
            elif method == "discard":
                gs.total_discards_used += 1

            detect_joker_changes(gs, state)

            gs.consecutive_errors = 0

            if stream_delay > 0:
                time.sleep(stream_delay)
        except httpx.TimeoutException:
            if method == "play":
                gs.total_hands_played += 1  # hand was played, game processed it
            log.warning("Timeout on %s(%s) — re-polling gamestate", method, params)
            try:
                state = client.call("gamestate")
                if _boss_disabled_before:
                    state["_boss_disabled"] = True
                # Check if game ended during the timeout
                if state.get("state") == "GAME_OVER":
                    break
                # Victory detection: if ante advanced to 9+ during a play
                # timeout, the win screen caused the hang — handle it here
                _check_victory(gs, state, pre_play_chips, expected_hand_score)
                # Diagnostic dump on timeout
                post_state = state.get("state", "?")
                pack_cards = state.get("pack", {}).get("cards", [])
                pack_labels = [c.get("label", "?") for c in pack_cards]
                log.warning(
                    "  post-timeout state=%s pack=%s money=%s ante=%s round=%s",
                    post_state, pack_labels or "none", state.get("money"),
                    state.get("ante_num"), state.get("round_num"),
                )
                if pack_cards:
                    for i, c in enumerate(pack_cards):
                        log.warning(
                            "  pack[%d]: label=%s key=%s set=%s",
                            i, c.get("label"), c.get("key"), c.get("set"),
                        )
                continue
            except httpx.TimeoutException:
                if cur_ante >= 8:
                    log.info(
                        "Server unresponsive at ante %d — likely post-win; exiting gracefully",
                        cur_ante,
                    )
                    gs.actually_won = True
                    break
                log.error("Double timeout — server unresponsive, aborting")
                raise
        except APIError as e:
            gs.consecutive_errors += 1
            log.warning("API error: %s (%s) — retry %d", e.message, e.name, gs.consecutive_errors)

            # If a consumable use was rejected, clear Fool tracking so
            # the rule engine doesn't keep trying the same consumable.
            if method == "use" and e.name == "NOT_ALLOWED":
                for rule in engine.rules.get(game_state, []):
                    if hasattr(rule, "_last_used_consumable"):
                        rule._last_used_consumable = None
                        break

            if gs.consecutive_errors >= 5:
                log.error("Too many consecutive errors, forcing skip")
                if game_state in ("TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK",
                                  "STANDARD_PACK", "BUFFOON_PACK", "SMODS_BOOSTER_OPENED"):
                    try:
                        state = client.call("pack", {"skip": True})
                    except APIError:
                        pass
                state = client.call("gamestate")
                if _boss_disabled_before:
                    state["_boss_disabled"] = True
                gs.consecutive_errors = 0
            else:
                time.sleep(poll_interval)
                state = client.call("gamestate")
                if _boss_disabled_before:
                    state["_boss_disabled"] = True
        finally:
            if saved_timeout is not None:
                client.timeout = saved_timeout

    log_game_over(gs, state)
    return gs.actually_won
