"""Main bot loop — connects to balatrobot and runs the decision engine."""

from __future__ import annotations

import logging
import logging.handlers
import re
import time
from collections import Counter
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
    ante_at_blind_start: int = 0
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
    round_results: list = field(default_factory=list)
    jokers_bought: list = field(default_factory=list)
    hand_types_played: Counter = field(default_factory=Counter)
    joker_scaling: dict = field(default_factory=dict)  # {name: {chips, mult, xmult}}
    consumables_bought: list = field(default_factory=list)  # [{name, type, count}]
    action_log: list = field(default_factory=list)  # [{seq, game_state, action_type, ...}]
    ante_snapshots: list = field(default_factory=list)  # [{ante, money, joker_roster, ...}]
    hand_scores: list = field(default_factory=list)  # [{seq, ante, hand_type, ...}]
    shop_events: list = field(default_factory=list)  # [{ante, event_type, item_name, ...}]
    hands_scored: int = 0  # sequence counter for hand_scores
    packs_opened: int = 0  # incremented on transitions into a pack-opened state
    in_pack_state: bool = False


class WinCaptureHandler(logging.handlers.MemoryHandler):
    """Buffers all log records; on flush_win() writes them to wins.txt."""

    def __init__(self, fmt: logging.Formatter, wins_file: str = "wins.txt") -> None:
        super().__init__(capacity=100_000, flushLevel=logging.CRITICAL + 1, target=None)
        self.fmt = fmt
        self.wins_file = wins_file

    def shouldFlush(self, record: logging.LogRecord) -> bool:
        return False

    def get_log_text(self) -> str:
        """Return buffered log records as a single string."""
        return "\n".join(self.fmt.format(record) for record in self.buffer)

    def flush_win(self, seed: str) -> None:
        with open(self.wins_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"VICTORY — seed {seed}\n")
            f.write(f"{'='*60}\n")
            for record in self.buffer:
                f.write(self.fmt.format(record) + "\n")
        # Don't clear buffer here — bot.py captures log_text after this

    def reset(self) -> None:
        self.buffer.clear()


_win_handler: WinCaptureHandler | None = None


def _compute_live_stats(gs: "GameLoopState"):
    """Snapshot per-run averages from observed bot activity (issue #41).

    Returns a ``LiveRunStats`` populated from ``gs``, or ``None`` when not enough
    data has accumulated (rounds/antes still at zero) — callers then fall back
    to ``LifetimeState``'s conservative defaults.
    """
    from balatro_bot.domain.policy.sim_context import LiveRunStats

    rounds_played = len(gs.round_results)
    if rounds_played <= 0:
        return None

    joker_sells = sum(
        1 for ev in gs.shop_events
        if ev.get("event_type") == "sell" and ev.get("item_type") == "joker"
    )
    # Antes are 1-indexed; use the highest ante we've actually seen play end in.
    antes_played = max((r.get("ante", 0) for r in gs.round_results), default=0)
    antes_played = max(1, antes_played)

    return LiveRunStats(
        avg_discards_per_round=gs.total_discards_used / rounds_played,
        avg_sells_per_ante=joker_sells / antes_played,
        avg_plays_per_round=gs.total_hands_played / rounds_played,
    )


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
    pre_play_chips: int = 0,
) -> bool:
    """Detect ante 9+ victory. Returns True if newly detected."""
    from balatro_bot.bot_logging import log_blind_result

    ante = state.get("ante_num", 0)
    if ante < 9 or gs.actually_won:
        return False

    gs.actually_won = True

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
    dashboard_batch_id: int | None = None,
) -> bool:
    """Main bot loop. Returns True if the game was won."""
    from balatro_bot.bot_logging import (
        build_play_snapshot, compute_played_hand_detail, detect_joker_changes,
        log_action, log_ante_transition, log_blind_transition, log_game_over,
        log_hand_state, log_played_hand, log_shop_state,
        serialize_joker_contributions,
    )

    if start_game:
        try:
            client.call("menu")
        except APIError:
            pass
        start_params: dict[str, object] = {"deck": deck, "stake": stake}
        if seed is not None:
            start_params["seed"] = seed
        for _attempt in range(3):
            try:
                state = client.call("start", start_params)
                break
            except (APIError, httpx.HTTPError) as e:
                reason = e.message if isinstance(e, APIError) else f"{type(e).__name__}: {e}"
                log.warning("start() failed (attempt %d): %s — retrying", _attempt + 1, reason)
                time.sleep(1)
                try:
                    client.call("menu")
                except (APIError, httpx.HTTPError):
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

        # Detect entry into a pack-opened state (any *_PACK or SMODS_BOOSTER_OPENED).
        # Counts every pack opened, whether bought, free from tags, or from effects.
        _is_pack_state = game_state in (
            "TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK",
            "STANDARD_PACK", "BUFFOON_PACK", "SMODS_BOOSTER_OPENED",
        )
        if _is_pack_state and not gs.in_pack_state:
            gs.packs_opened += 1
        gs.in_pack_state = _is_pack_state

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

        state["_live_stats"] = _compute_live_stats(gs)
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

        # Capture joker/consumable label before buy RPC (state changes after call)
        _buying_joker_label = None
        _buying_consumable = None
        _buying_pack_label = None
        _picking_pack_card_label = None
        if method == "buy" and params and "card" in params:
            shop_cards = state.get("shop", {}).get("cards", [])
            idx = params["card"]
            if idx < len(shop_cards):
                card_set = shop_cards[idx].get("set", "")
                card_label = shop_cards[idx].get("label", "?")
                if card_set == "JOKER":
                    _buying_joker_label = card_label
                elif card_set in ("TAROT", "PLANET", "SPECTRAL"):
                    _buying_consumable = {"name": card_label, "type": card_set.lower()}
        # Voucher buys use a separate "card" index in vouchers list
        if method == "buy" and params and "voucher" in params:
            vouchers = state.get("vouchers", {}).get("cards", [])
            idx = params["voucher"]
            if idx < len(vouchers):
                _buying_consumable = {"name": vouchers[idx].get("label", "?"), "type": "voucher"}
        # Pack buys use {pack: i} indexing into state["packs"]["cards"]
        if method == "buy" and params and "pack" in params:
            packs_list = state.get("packs", {}).get("cards", [])
            pidx = params["pack"]
            if pidx < len(packs_list):
                _buying_pack_label = packs_list[pidx].get("label", "?")
        # Pack picks: {card: i} indexing into state["pack"]["cards"]
        if method == "pack" and params and "card" in params:
            pack_open = state.get("pack", {}).get("cards", [])
            cidx = params["card"]
            if cidx < len(pack_open):
                _picking_pack_card_label = pack_open[cidx].get("label", "?")

        pre_play_chips = 0
        play_snapshot = None
        _pre_money = state.get("money", 0)
        _pre_chips = state.get("round", {}).get("chips", 0)
        _pre_state_hand = state.get("hand", {}).get("cards", []) if method == "use" else None
        _using_consumable_name = None
        if method == "use" and params and "consumable" in params:
            consumables_list = state.get("consumables", {}).get("cards", [])
            cidx = params["consumable"]
            if cidx < len(consumables_list):
                _using_consumable_name = consumables_list[cidx].get("label", "?")
        if method == "play":
            pre_play_chips = _pre_chips
            _scoring_log.info("  PRE_PLAY chips_in_round=%d", pre_play_chips)
            play_snapshot = build_play_snapshot(state, params, action)

        try:
            _boss_disabled_before = state.get("_boss_disabled", False)
            state = client.call(method, params)
            # Preserve _boss_disabled across state replacement — the API
            # never reports blind.disabled, so this side-channel flag
            # (set when Luchador is sold) would otherwise be lost.
            if _boss_disabled_before:
                state["_boss_disabled"] = True
            gs.actions_taken += 1
            # Enrich action detail for consumable uses with card labels
            _action_detail = params
            if method == "use" and params and "cards" in params:
                # Resolve target card indices to labels from pre-call state
                hand_cards = _pre_state_hand or []
                target_labels = []
                for ci in params["cards"]:
                    if ci < len(hand_cards):
                        c = hand_cards[ci]
                        rank = c.get("value", {}).get("rank", "?")
                        suit = c.get("value", {}).get("suit", "?")
                        target_labels.append(f"{rank} of {suit}")
                _action_detail = dict(params, target_labels=target_labels)
            if method == "use" and params and "consumable" in params:
                # Add the consumable name
                _action_detail = dict(_action_detail or params,
                                      consumable_name=_using_consumable_name)
            gs.action_log.append({
                "seq": gs.actions_taken,
                "game_state": game_state,
                "action_type": method,
                "detail": _action_detail,
                "ante": state.get("ante_num"),
                "blind_name": gs.current_blind_name,
                "chips_before": _pre_chips,
                "chips_after": state.get("round", {}).get("chips", 0),
                "money_before": _pre_money,
                "money_after": state.get("money", 0),
            })
            if method == "play":
                gs.total_hands_played += 1
                hand_name = getattr(action, "hand_name", "") or ""
                if hand_name:
                    gs.hand_types_played[hand_name] += 1
                post_chips = state.get("round", {}).get("chips", 0)
                _scoring_log.info("  POST_PLAY chips_in_round=%d, delta=%d", post_chips, post_chips - pre_play_chips)
                # The Hook discards 2 held cards BEFORE scoring (before
                # cards are replenished).  The pre-play snapshot is used
                # as-is; joker corrections (Ramen, Yorick, Green Joker)
                # are applied in joker_effects/complex.py.
                log_played_hand(play_snapshot, pre_play_chips, state)
                # Capture hand score for dashboard
                gs.hands_scored += 1
                actual_score = post_chips - pre_play_chips
                joker_contribs = None
                try:
                    _detail = compute_played_hand_detail(play_snapshot)
                    joker_contribs = serialize_joker_contributions(_detail)
                except Exception:
                    joker_contribs = None
                gs.hand_scores.append({
                    "seq": gs.hands_scored,
                    "ante": state.get("ante_num"),
                    "blind_name": gs.current_blind_name,
                    "hand_type": hand_name or None,
                    "total_score": actual_score if actual_score > 0 else None,
                    "joker_contributions": joker_contribs,
                })
            elif method == "discard":
                gs.total_discards_used += 1

            if _buying_joker_label:
                gs.jokers_bought.append({
                    "joker_name": _buying_joker_label,
                    "buy_ante": state.get("ante_num", 0),
                })

            if _buying_consumable:
                # Aggregate by name+type
                name = _buying_consumable["name"]
                ctype = _buying_consumable["type"]
                for c in gs.consumables_bought:
                    if c["name"] == name and c["consumable_type"] == ctype:
                        c["count"] += 1
                        break
                else:
                    gs.consumables_bought.append({
                        "name": name,
                        "consumable_type": ctype,
                        "count": 1,
                    })

            # Track shop events: buys, sells, rerolls
            _post_money = state.get("money", 0)
            _ante = state.get("ante_num")
            if _buying_joker_label:
                gs.shop_events.append({
                    "ante": _ante, "event_type": "buy",
                    "item_name": _buying_joker_label, "item_type": "joker",
                    "cost": _pre_money - _post_money, "money_after": _post_money,
                })
            if _buying_consumable:
                gs.shop_events.append({
                    "ante": _ante, "event_type": "buy",
                    "item_name": _buying_consumable["name"],
                    "item_type": _buying_consumable["type"],
                    "cost": _pre_money - _post_money, "money_after": _post_money,
                })
            if method == "sell":
                sold_name = getattr(action, "reason", "") or "?"
                if hasattr(action, "index"):
                    # Try to get the item name from pre-state
                    if "joker" in (params or {}):
                        jokers = state.get("jokers", {}).get("cards", [])
                        sold_name = "joker"
                    elif "consumable" in (params or {}):
                        sold_name = "consumable"
                gs.shop_events.append({
                    "ante": _ante, "event_type": "sell",
                    "item_name": sold_name, "item_type": None,
                    "cost": None, "money_after": _post_money,
                })
            if method == "reroll":
                gs.shop_events.append({
                    "ante": _ante, "event_type": "reroll",
                    "item_name": None, "item_type": None,
                    "cost": _pre_money - _post_money, "money_after": _post_money,
                })
            if _buying_pack_label is not None:
                gs.shop_events.append({
                    "ante": _ante, "event_type": "buy",
                    "item_name": _buying_pack_label, "item_type": "pack",
                    "cost": _pre_money - _post_money, "money_after": _post_money,
                })
            if method == "pack" and params:
                if params.get("skip"):
                    gs.shop_events.append({
                        "ante": _ante, "event_type": "skip",
                        "item_name": None, "item_type": "pack",
                        "cost": None, "money_after": _post_money,
                    })
                elif "card" in params:
                    gs.shop_events.append({
                        "ante": _ante, "event_type": "pick",
                        "item_name": _picking_pack_card_label,
                        "item_type": "pack",
                        "cost": None, "money_after": _post_money,
                    })

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
                _check_victory(gs, state, pre_play_chips)
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
                log.error("Double timeout — server unresponsive, aborting")
                raise
        except APIError as e:
            gs.consecutive_errors += 1
            log.warning("API error: %s (%s) — retry %d", e.message, e.name, gs.consecutive_errors)

            # If a consumable use was rejected, block that index so the
            # rule engine doesn't keep trying the same consumable this round.
            if method == "use" and e.name == "NOT_ALLOWED":
                blocked_idx = (params or {}).get("consumable")
                for rule in engine.rules.get(game_state, []):
                    if hasattr(rule, "_last_used_consumable"):
                        rule._last_used_consumable = None
                    if hasattr(rule, "_blocked_consumables") and blocked_idx is not None:
                        rule._blocked_consumables.add(blocked_idx)
                        log.info("Blocked consumable index %d for this round", blocked_idx)
                        break

            # If a joker buy was rejected due to full slots, tell the
            # shop evaluator so it stops trying to buy jokers this visit.
            if "joker slots are full" in e.message:
                for rule in engine.rules.get("SHOP", []):
                    evaluator = getattr(rule, "_evaluator", None)
                    if evaluator is not None:
                        evaluator.slots_full = True
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
    log_game_over(gs, state)

    # Capture log text AFTER log_game_over (includes game summary lines).
    # flush_win() no longer clears the buffer, so it's intact for both wins and losses.
    _captured_log_text = _win_handler.get_log_text() if _win_handler else None
    if _win_handler:
        _win_handler.reset()

    # POST game data to dashboard
    if dashboard_batch_id:
        from balatro_bot import dashboard_client
        joker_cards = state.get("jokers", {}).get("cards", [])
        final_roster = {j.get("label", "?") for j in joker_cards}
        _rerolls = sum(1 for e in gs.shop_events if e.get("event_type") == "reroll")
        _packs_bought = sum(1 for e in gs.shop_events if e.get("event_type") == "buy" and e.get("item_type") == "pack")
        _pack_picks = sum(1 for e in gs.shop_events if e.get("event_type") == "pick" and e.get("item_type") == "pack")
        _pack_skips = sum(1 for e in gs.shop_events if e.get("event_type") == "skip" and e.get("item_type") == "pack")
        dashboard_client.post_game(dashboard_batch_id, {
            "instance_port": client.port,
            "seed": state.get("seed", ""),
            "deck": deck,
            "stake": stake,
            "win": gs.actually_won,
            "final_ante": state.get("ante_num", 0),
            "final_round": state.get("round_num", 0),
            "actions": gs.actions_taken,
            "rerolls": _rerolls,
            "packs_bought": _packs_bought,
            "packs_opened": gs.packs_opened,
            "pack_picks": _pack_picks,
            "pack_skips": _pack_skips,
            "final_money": state.get("money", 0),
            "final_jokers": ", ".join(j.get("label", "?") for j in joker_cards),
            "total_hands": gs.total_hands_played,
            "total_discards": gs.total_discards_used,
            "log_text": _captured_log_text,
            "rounds": gs.round_results,
            "jokers": [
                {
                    "joker_name": jb["joker_name"],
                    "in_final_roster": jb["joker_name"] in final_roster,
                    "buy_ante": jb["buy_ante"],
                    "final_chips": (gs.joker_scaling.get(jb["joker_name"]) or {}).get("chips"),
                    "final_mult": (gs.joker_scaling.get(jb["joker_name"]) or {}).get("mult"),
                    "final_xmult": (gs.joker_scaling.get(jb["joker_name"]) or {}).get("xmult"),
                }
                for jb in gs.jokers_bought
            ],
            "hand_types": dict(gs.hand_types_played),
            "consumables": gs.consumables_bought,
            "actions_log": gs.action_log,
            "ante_snapshots": gs.ante_snapshots,
            "hand_scores": gs.hand_scores,
            "shop_events": gs.shop_events,
        })

    return gs.actually_won
