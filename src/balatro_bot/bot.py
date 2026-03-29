"""Main bot loop — connects to balatrobot and runs the decision engine."""

from __future__ import annotations

import logging
import logging.handlers
import random
import string
import subprocess
import time
from typing import TYPE_CHECKING

import httpx
from balatrobot import APIError, BalatroClient

from balatro_bot.engine import RuleEngine

if TYPE_CHECKING:
    pass

log = logging.getLogger("balatro_bot")


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


_server_proc: subprocess.Popen | None = None


def restart_balatro_server(
    port: int,
    uvx: str,
    love_path: str,
    lovely_path: str,
    wait_secs: float = 15.0,
) -> None:
    """Kill the dead balatrobot process on this port and spawn a fresh one."""
    global _server_proc
    log.info("Restarting balatrobot server on port %d...", port)

    # Kill the tracked process directly (handles crashed servers that netstat can't find)
    if _server_proc is not None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(_server_proc.pid)],
                capture_output=True,
            )
            log.info("Killed tracked server PID %s", _server_proc.pid)
        except Exception:
            pass
        _server_proc = None

    # Also sweep netstat for anything else on the port (orphans from previous sessions)
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line and ("LISTENING" in line or "ESTABLISHED" in line):
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and pid != "0":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
                    log.info("Killed PID %s on port %d", pid, port)
    except Exception as e:
        log.warning("Could not kill old process: %s", e)

    time.sleep(2.0)

    _server_proc = subprocess.Popen(
        [uvx, "balatrobot", "serve",
         "--port", str(port),
         "--headless", "--fast",
         "--love-path", love_path,
         "--lovely-path", lovely_path],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    log.info("Spawned new balatrobot server on port %d (PID %s), waiting %.0fs...",
             port, _server_proc.pid, wait_secs)
    time.sleep(wait_secs)


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


_scoring_log = logging.getLogger("balatro_scoring")


def _log_played_hand(snapshot: dict | None, pre_chips: int, new_state: dict, fmt_card) -> None:
    """Log a detailed scoring breakdown for a hand that was just played."""
    if not snapshot or not _scoring_log.handlers:
        return
    try:
        from balatro_bot.hand_evaluator import score_hand_detailed, classify_hand, _scoring_cards_for

        played = snapshot["played"]
        hand_name = snapshot["hand_name"]
        if not hand_name:
            hand_name = classify_hand(played)

        joker_keys = {j.get("key") for j in snapshot["jokers"]}
        has_splash = "j_splash" in joker_keys
        scoring = played if has_splash else _scoring_cards_for(hand_name, played)

        detail = score_hand_detailed(
            hand_name, scoring,
            hand_levels=snapshot["hand_levels"],
            jokers=snapshot["jokers"],
            played_cards=played,
            held_cards=snapshot["held"],
            money=snapshot["money"],
            discards_left=snapshot["discards_left"],
            hands_left=snapshot["hands_left"],
            joker_limit=snapshot["joker_limit"],
            ancient_suit=snapshot.get("ancient_suit"),
        )

        post_chips = new_state.get("round", {}).get("chips", 0)
        actual_chips = post_chips - pre_chips
        actual_reliable = actual_chips > 0
        cards_str = ", ".join(fmt_card(c) for c in played)

        joker_parts = []
        for label, dc, dm in detail["joker_contributions"]:
            parts = []
            if dc:
                parts.append(f"+{dc:.0f}c")
            if dm:
                parts.append(f"+{dm:.1f}m")
            if parts:
                joker_parts.append(f"{label}({', '.join(parts)})")
        joker_str = ", ".join(joker_parts) if joker_parts else "none"

        mismatch = ""
        if actual_reliable and detail["total"] != actual_chips:
            mismatch = f" MISMATCH(diff={actual_chips - detail['total']:+d})"
        elif not actual_reliable:
            mismatch = " (actual unreliable)"

        _scoring_log.info(
            "%s [%s] | base: %d/%d | pre-joker: %d/%.1f | jokers: [%s] | "
            "final: %d/%.1f | est=%d actual=%d%s",
            detail["hand_name"], cards_str,
            detail["base_chips"], detail["base_mult"],
            detail["pre_joker_chips"], detail["pre_joker_mult"],
            joker_str,
            detail["post_joker_chips"], detail["post_joker_mult"],
            detail["total"], actual_chips,
            mismatch,
        )
    except Exception as e:
        _scoring_log.warning("scoring log error: %s", e)


def run_bot(
    client: BalatroClient,
    engine: RuleEngine,
    *,
    start_game: bool = False,
    deck: str = "RED",
    stake: str = "WHITE",
    seed: str | None = None,
    poll_interval: float = 0.2,
) -> bool:
    """Main bot loop. Returns True if the game was won."""
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
    else:
        state = client.call("gamestate")
        log.info("Joined existing game: state=%s", state.get("state"))

    actions_taken = 0
    consecutive_errors = 0
    total_hands_played = 0
    total_discards_used = 0
    last_logged_hand = None
    last_logged_shop = None
    last_logged_blind = None
    prev_hand_labels: list[str] = []
    last_ante = None
    last_round = None
    current_blind_name = None
    current_blind_target = None
    pre_blind_chips = 0

    SUIT_SYM = {"H": "\u2665", "D": "\u2666", "C": "\u2663", "S": "\u2660"}
    RANK_SYM = {
        "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
        "7": "7", "8": "8", "9": "9", "T": "10",
        "J": "J", "Q": "Q", "K": "K", "A": "A",
    }

    def fmt_card(c: dict) -> str:
        val = c.get("value", {})
        return RANK_SYM.get(val.get("rank", ""), "?") + SUIT_SYM.get(val.get("suit", ""), "?")

    while state.get("state") != "GAME_OVER":
        game_state = state.get("state", "")

        ante_num = state.get("ante_num")
        round_num = state.get("round_num")
        if ante_num is not None and ante_num != last_ante:
            joker_cards = state.get("jokers", {}).get("cards", [])
            roster = [j.get("label", "?") for j in joker_cards]
            log.info(
                "Ante %s roster (%d jokers): [%s]",
                ante_num, len(roster),
                ", ".join(roster) if roster else "none",
            )
            last_ante = ante_num
        if last_round is not None and round_num != last_round and current_blind_name:
            final_chips = state.get("round", {}).get("chips", 0)
            scored = final_chips - pre_blind_chips
            log.info(
                "Blind cleared: %s | scored %s / needed %s",
                current_blind_name, scored, current_blind_target,
            )
            current_blind_name = None
            current_blind_target = None
            pre_blind_chips = 0
        last_round = round_num

        if game_state in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "SPLASH", "TUTORIAL"):
            last_logged_hand = None
            time.sleep(poll_interval)
            state = client.call("gamestate")
            continue

        if game_state != "SELECTING_HAND":
            last_logged_hand = None
            prev_hand_labels = []

        if game_state != "SHOP":
            last_logged_shop = None

        if game_state == "SHOP":
            shop_cards = state.get("shop", {}).get("cards", [])
            packs = state.get("packs", {}).get("cards", [])
            vouchers = state.get("vouchers", {}).get("cards", [])
            shop_id = tuple(c.get("label", "") for c in shop_cards)
            if shop_id != last_logged_shop:
                jokers_avail = [f"{c.get('label','?')}(${c.get('cost',{}).get('buy','?')})"
                                for c in shop_cards if c.get("set") == "JOKER"]
                consumables_avail = [f"{c.get('label','?')}(${c.get('cost',{}).get('buy','?')})"
                                     for c in shop_cards if c.get("set") not in ("JOKER",)]
                packs_avail = [f"{c.get('label','?')}(${c.get('cost',{}).get('buy','?')})"
                               for c in packs]
                vouchers_avail = [f"{c.get('label','?')}(${c.get('cost',{}).get('buy','?')})"
                                  for c in vouchers]
                parts = []
                if jokers_avail:
                    parts.append("jokers: " + ", ".join(jokers_avail))
                if consumables_avail:
                    parts.append("consumables: " + ", ".join(consumables_avail))
                if packs_avail:
                    parts.append("packs: " + ", ".join(packs_avail))
                if vouchers_avail:
                    parts.append("vouchers: " + ", ".join(vouchers_avail))
                money = state.get("money", 0)
                log.info("Shop ($%d): %s", money, " | ".join(parts) if parts else "(empty)")
                last_logged_shop = shop_id

        if game_state == "BLIND_SELECT":
            blinds = state.get("blinds", {})
            blind_id = tuple(
                (k, b.get("name", ""), b.get("status", ""))
                for k, b in blinds.items() if isinstance(b, dict)
            )
            if blind_id != last_logged_blind:
                for key, b in blinds.items():
                    if isinstance(b, dict) and b.get("status") == "CURRENT":
                        name = b.get("name", "?")
                        score = b.get("score", "?")
                        log.info("Blind: %s (need %s chips)", name, score)
                        current_blind_name = name
                        current_blind_target = score
                        pre_blind_chips = state.get("round", {}).get("chips", 0)
                        break
                last_logged_blind = blind_id

        if game_state != "BLIND_SELECT":
            last_logged_blind = None

        if game_state == "SELECTING_HAND":
            hand_cards = state.get("hand", {}).get("cards", [])
            hand_id = tuple(c.get("label", "") for c in hand_cards)
            if hand_id != last_logged_hand:
                hand_str = ", ".join(fmt_card(c) for c in hand_cards)
                if prev_hand_labels:
                    remaining = list(prev_hand_labels)
                    drew = []
                    for c in hand_cards:
                        lbl = c.get("label", "")
                        if lbl in remaining:
                            remaining.remove(lbl)
                        else:
                            drew.append(fmt_card(c))
                    drew_str = f" | drew: [{', '.join(drew)}]" if drew else ""
                else:
                    drew_str = ""
                debuffed = [fmt_card(c) for c in hand_cards
                            if isinstance(c.get("state", {}), dict) and c["state"].get("debuff")]
                debuff_str = f" | DEBUFFED: [{', '.join(debuffed)}]" if debuffed else ""
                log.info("Hand: [%s]%s%s", hand_str, drew_str, debuff_str)
                prev_hand_labels = [c.get("label", "") for c in hand_cards]
                last_logged_hand = hand_id

        action = engine.decide(state)
        if action is None:
            log.debug("No rule matched for state=%s, polling...", game_state)
            time.sleep(poll_interval)
            state = client.call("gamestate")
            continue

        method, params = action.to_rpc()

        card_detail = ""
        if method in ("play", "discard") and "cards" in (params or {}):
            hand_cards = state.get("hand", {}).get("cards", [])
            indices = params["cards"]
            labels = [fmt_card(hand_cards[i]) for i in indices if i < len(hand_cards)]
            card_detail = f" [{', '.join(labels)}]"

        log.info(
            "[#%d] %s -> %s(%s)%s | %s",
            actions_taken,
            game_state,
            method,
            params or "",
            card_detail,
            getattr(action, "reason", ""),
        )

        pre_play_chips = 0
        play_snapshot = None
        if method == "play":
            pre_play_chips = state.get("round", {}).get("chips", 0)
            hand_cards_snap = state.get("hand", {}).get("cards", [])
            play_indices = set(params.get("cards", []))
            play_snapshot = {
                "played": [hand_cards_snap[i] for i in params.get("cards", []) if i < len(hand_cards_snap)],
                "held": [c for j, c in enumerate(hand_cards_snap) if j not in play_indices],
                "jokers": state.get("jokers", {}).get("cards", []),
                "hand_levels": state.get("hands", {}),
                "money": state.get("money", 0),
                "discards_left": state.get("round", {}).get("discards_left", 0),
                "hands_left": state.get("round", {}).get("hands_left", 1),
                "joker_limit": state.get("jokers", {}).get("limit", 5),
                "hand_name": getattr(action, "hand_name", ""),
                "ancient_suit": state.get("round", {}).get("ancient_suit"),
            }

        try:
            state = client.call(method, params)
            actions_taken += 1
            if method == "play":
                total_hands_played += 1
                _log_played_hand(play_snapshot, pre_play_chips, state, fmt_card)
            elif method == "discard":
                total_discards_used += 1
            consecutive_errors = 0
        except httpx.TimeoutException:
            raise
        except APIError as e:
            consecutive_errors += 1
            log.warning("API error: %s (%s) — retry %d", e.message, e.name, consecutive_errors)
            if consecutive_errors >= 5:
                log.error("Too many consecutive errors, forcing skip")
                try:
                    state = client.call("pack", {"skip": True})
                except APIError:
                    pass
                state = client.call("gamestate")
                consecutive_errors = 0
            else:
                time.sleep(poll_interval)
                state = client.call("gamestate")

    won = state.get("won", False)
    ante = state.get("ante_num", "?")
    round_num = state.get("round_num", "?")
    seed = state.get("seed", "?")
    log.info(
        "Game over: %s | seed=%s ante=%s round=%s | %d actions taken",
        "VICTORY" if won else "DEFEAT",
        seed, ante, round_num, actions_taken,
    )
    joker_names = [j.get("label", "?") for j in state.get("jokers", {}).get("cards", [])]
    log.info(
        "Summary: $%d | jokers: [%s] | hands=%d discards=%d",
        state.get("money", 0),
        ", ".join(joker_names) if joker_names else "none",
        total_hands_played, total_discards_used,
    )

    if _win_handler:
        if won:
            _win_handler.flush_win(seed)
        else:
            _win_handler.reset()

    return won
