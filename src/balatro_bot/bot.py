"""Main bot loop — connects to balatrobot and runs the decision engine."""

from __future__ import annotations

import logging
import logging.handlers
import random
import string
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
    hands_at_blind_start = 0
    discards_at_blind_start = 0
    last_logged_hand = None
    last_logged_shop = None
    last_logged_blind = None
    prev_joker_keys: set[str] = set()
    hand_context_logged = False
    prev_hand_labels: list[str] = []
    last_ante = None
    last_round = None
    current_blind_name = None
    current_blind_target = None
    max_chips_this_blind = 0

    SUIT_SYM = {"H": "\u2665", "D": "\u2666", "C": "\u2663", "S": "\u2660"}
    RANK_SYM = {
        "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
        "7": "7", "8": "8", "9": "9", "T": "10",
        "J": "J", "Q": "Q", "K": "K", "A": "A",
    }

    def fmt_card(c: dict) -> str:
        val = c.get("value", {})
        return RANK_SYM.get(val.get("rank", ""), "?") + SUIT_SYM.get(val.get("suit", ""), "?")

    won_logged = False
    actually_won = False  # True only when bot reaches ante 9+ (beat the ante 8 boss)

    while state.get("state") != "GAME_OVER":
        game_state = state.get("state", "")

        # Detect real win: bot advanced past ante 8 (beat the boss)
        # Don't trust state.won — Hieroglyph voucher inflates ante counter
        cur_ante = state.get("ante_num", 0)
        if cur_ante >= 9 and not actually_won:
            actually_won = True
            log.info(
                "VICTORY at ante %s round %s (seed=%s) — beat Ante 8 boss, entering endless",
                cur_ante, state.get("round_num", "?"),
                state.get("seed", "?"),
            )

        # Log when state.won fires (may be premature due to Hieroglyph)
        if state.get("won") and not won_logged:
            if not actually_won:
                log.info(
                    "state.won=true at ante %s (Hieroglyph?) — not a real win until ante 9+",
                    state.get("ante_num", "?"),
                )
            won_logged = True

        ante_num = state.get("ante_num")
        round_num = state.get("round_num")
        if ante_num is not None and ante_num != last_ante:
            joker_cards = state.get("jokers", {}).get("cards", [])
            hand_levels = state.get("hands", {})
            money = state.get("money", 0)

            # Roster with effect values
            roster_parts = []
            for j in joker_cards:
                label = j.get("label", "?")
                effect_text = j.get("value", {}).get("effect", "")
                # Extract compact value from effect text
                from balatro_bot.joker_effects import parse_effect_value
                parsed = parse_effect_value(effect_text) if effect_text else {}
                if parsed.get("xmult"):
                    roster_parts.append(f"{label}(X{parsed['xmult']:.1f})")
                elif parsed.get("mult"):
                    roster_parts.append(f"{label}(+{parsed['mult']:.0f}mult)")
                elif parsed.get("chips"):
                    roster_parts.append(f"{label}(+{parsed['chips']:.0f}chips)")
                else:
                    roster_parts.append(label)

            log.info(
                "[ANTE %s] Roster (%d jokers): [%s]",
                ante_num, len(joker_cards),
                ", ".join(roster_parts) if roster_parts else "none",
            )

            # Strategy snapshot
            from balatro_bot.strategy import compute_strategy
            strat = compute_strategy(joker_cards, hand_levels)
            log.info("[ANTE %s] Strategy: %s", ante_num, strat.describes())

            # Hand levels (only leveled-up hands)
            leveled = []
            for ht, data in hand_levels.items():
                if isinstance(data, dict) and data.get("level", 1) > 1:
                    leveled.append(f"{ht}(lv{data['level']})")
            if leveled:
                log.info("[ANTE %s] Money: $%d | Levels: %s", ante_num, money, ", ".join(leveled))
            else:
                log.info("[ANTE %s] Money: $%d", ante_num, money)

            last_ante = ante_num
        # Track highest chips seen during this blind
        # Track highest chips seen during this blind (capture before round resets)
        cur_chips = state.get("round", {}).get("chips", 0)
        if cur_chips > max_chips_this_blind:
            max_chips_this_blind = cur_chips
        if last_round is not None and round_num != last_round and current_blind_name:
            log.info(
                "[ROUND] %s: scored %s / needed %s — WON | %d hands, %d discards",
                current_blind_name, f"{max_chips_this_blind:,}",
                f"{current_blind_target:,}" if isinstance(current_blind_target, int) else current_blind_target,
                total_hands_played - hands_at_blind_start,
                total_discards_used - discards_at_blind_start,
            )
            current_blind_name = None
            current_blind_target = None
            max_chips_this_blind = 0
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
                    if isinstance(b, dict) and b.get("status") in ("SELECT", "CURRENT"):
                        name = b.get("name", "?")
                        score = b.get("score", "?")
                        log.info("Blind: %s (need %s chips)", name, score)
                        current_blind_name = name
                        current_blind_target = score
                        max_chips_this_blind = 0
                        hands_at_blind_start = total_hands_played
                        discards_at_blind_start = total_discards_used
                        hand_context_logged = False
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

                # First hand of a new blind — log context once
                if not hand_context_logged and current_blind_name:
                    jokers_now = state.get("jokers", {}).get("cards", [])
                    hand_levels_now = state.get("hands", {})
                    rnd = state.get("round", {})
                    from balatro_bot.hand_evaluator import best_hand as _bh
                    jlimit = state.get("jokers", {}).get("limit", 5)
                    bh = _bh(hand_cards, hand_levels_now, jokers=jokers_now, joker_limit=jlimit)
                    best_score = bh.total if bh else 0
                    best_name = bh.hand_name if bh else "?"
                    blind_need = current_blind_target or 0
                    can_win = best_score >= blind_need if blind_need else False
                    log.info(
                        "[HAND] Best: %s for %s | Blind: %s needs %s | Hands: %d | %s",
                        best_name, f"{best_score:,}", current_blind_name,
                        f"{blind_need:,}" if isinstance(blind_need, int) else blind_need,
                        rnd.get("hands_left", 0),
                        "CAN WIN" if can_win else "NEED MORE",
                    )
                    hand_context_logged = True

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

            # Detect joker roster changes and log strategy shift
            cur_joker_keys = {j.get("key") for j in state.get("jokers", {}).get("cards", [])}
            if cur_joker_keys != prev_joker_keys and prev_joker_keys:
                from balatro_bot.strategy import compute_strategy as _cs
                new_strat = _cs(state.get("jokers", {}).get("cards", []), state.get("hands", {}))
                log.info("[STRAT] %s", new_strat.describes())
            prev_joker_keys = cur_joker_keys

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

    # Log final round summary if died on a blind
    if current_blind_name:
        log.info(
            "[ROUND] %s: scored %s / needed %s — LOST | %d hands, %d discards",
            current_blind_name, f"{max_chips_this_blind:,}",
            f"{current_blind_target:,}" if isinstance(current_blind_target, int) else current_blind_target,
            total_hands_played - hands_at_blind_start,
            total_discards_used - discards_at_blind_start,
        )

    ante = state.get("ante_num", "?")
    round_num = state.get("round_num", "?")
    seed = state.get("seed", "?")
    if actually_won:
        log.info(
            "Game over: VICTORY (died in endless) | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, actions_taken,
        )
    elif state.get("won", False):
        log.info(
            "Game over: DEFEAT (state.won=true but never reached ante 9) | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, actions_taken,
        )
    else:
        log.info(
            "Game over: DEFEAT | seed=%s ante=%s round=%s | %d actions taken",
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
        if actually_won:
            _win_handler.flush_win(seed)
        else:
            _win_handler.reset()

    return actually_won
