"""Game loop logging helpers — scoring diagnostics, state transitions, action summaries."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from balatro_bot.bot_format import fmt_card, format_deck_snapshot
from balatro_bot.cards import joker_key
from balatro_bot.domain.models.card import Card

if TYPE_CHECKING:
    from balatro_bot.bot import GameLoopState

log = logging.getLogger("balatro_bot")
_stream_log = logging.getLogger("balatro_stream")
_scoring_log = logging.getLogger("balatro_scoring")


# ---------------------------------------------------------------------------
# Round / blind result
# ---------------------------------------------------------------------------

def log_blind_result(gs: GameLoopState, result: str = "WON") -> None:
    """Log round summary for the current blind."""
    if not gs.current_blind_name:
        return
    target_str = (
        f"{gs.current_blind_target:,}"
        if isinstance(gs.current_blind_target, int)
        else gs.current_blind_target
    )
    hands_used = gs.total_hands_played - gs.hands_at_blind_start
    discards_used = gs.total_discards_used - gs.discards_at_blind_start
    log.info(
        "[ROUND] %s: scored %s / needed %s — %s | %d hands, %d discards",
        gs.current_blind_name, f"{gs.max_chips_this_blind:,}",
        target_str, result, hands_used, discards_used,
    )
    if result == "WON":
        _stream_log.info(
            "%s WON — %s / %s | %d hands, %d discards",
            gs.current_blind_name, f"{gs.max_chips_this_blind:,}",
            target_str, hands_used, discards_used,
        )

    # Accumulate for dashboard reporting
    is_boss = gs.current_blind_name.startswith("The ") if gs.current_blind_name else False
    gs.round_results.append({
        "ante": gs.last_ante or 0,
        "blind_name": gs.current_blind_name,
        "is_boss": is_boss,
        "scored": gs.max_chips_this_blind,
        "needed": gs.current_blind_target if isinstance(gs.current_blind_target, int) else 0,
        "won": result == "WON",
        "hands_used": hands_used,
        "discards_used": discards_used,
    })


# ---------------------------------------------------------------------------
# Per-state logging helpers
# ---------------------------------------------------------------------------

def log_ante_transition(gs: GameLoopState, state: dict) -> None:
    """Log roster, strategy, hand levels when ante changes."""
    ante_num = state.get("ante_num")
    if ante_num is None or ante_num == gs.last_ante:
        return

    joker_cards = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})
    money = state.get("money", 0)

    # Roster with effect values
    from balatro_bot.joker_effects import parse_effect_value
    roster_parts = []
    for j in joker_cards:
        label = j.get("label", "?")
        effect_text = j.get("value", {}).get("effect", "")
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

    deck_cards = state.get("cards", {}).get("cards", [])
    log.info("[ANTE %s] Deck %s", ante_num, format_deck_snapshot(deck_cards))

    from balatro_bot.strategy import compute_strategy
    strat = compute_strategy(joker_cards, hand_levels)
    log.info("[ANTE %s] Strategy: %s", ante_num, strat.describes())

    leveled = []
    for ht, data in hand_levels.items():
        if hasattr(data, "get") and data.get("level", 1) > 1:
            leveled.append(f"{ht}(lv{data['level']})")
    if leveled:
        log.info("[ANTE %s] Money: $%d | Levels: %s", ante_num, money, ", ".join(leveled))
    else:
        log.info("[ANTE %s] Money: $%d", ante_num, money)

    _stream_log.info("")
    _stream_log.info("=== Ante %s / 8 ===", ante_num)
    joker_names = [j.get("label", "?") for j in joker_cards]
    _stream_log.info("Jokers: %s", ", ".join(joker_names) if joker_names else "(none)")
    _stream_log.info("Money: $%d", money)

    gs.last_ante = ante_num


def log_shop_state(gs: GameLoopState, state: dict) -> None:
    """Log shop inventory when it changes."""
    shop_cards = state.get("shop", {}).get("cards", [])
    packs = state.get("packs", {}).get("cards", [])
    vouchers = state.get("vouchers", {}).get("cards", [])
    shop_id = tuple(c.get("label", "") for c in shop_cards)
    if shop_id == gs.last_logged_shop:
        return

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
    gs.last_logged_shop = shop_id


def log_blind_transition(gs: GameLoopState, state: dict) -> None:
    """Detect blind select changes, log previous blind result, set up new blind tracking."""
    blinds = state.get("blinds", {})
    blind_id = tuple(
        (k, b.get("name", ""), b.get("status", ""))
        for k, b in blinds.items() if isinstance(b, dict)
    )
    if blind_id == gs.last_logged_blind:
        return

    for key, b in blinds.items():
        if isinstance(b, dict) and b.get("status") in ("SELECT", "CURRENT"):
            name = b.get("name", "?")
            score = b.get("score", "?")
            # Log previous blind summary before overwriting
            if gs.current_blind_name and gs.max_chips_this_blind > 0:
                log_blind_result(gs, "WON")
            log.info("Blind: %s (need %s chips)", name, score)
            _stream_log.info("")
            _stream_log.info("--- %s — need %s chips ---", name, f"{score:,}" if isinstance(score, int) else score)
            gs.current_blind_name = name
            gs.current_blind_target = score
            gs.max_chips_this_blind = 0
            state.pop("_boss_disabled", None)
            gs.hands_at_blind_start = gs.total_hands_played
            gs.discards_at_blind_start = gs.total_discards_used
            gs.hand_context_logged = False
            break
    gs.last_logged_blind = blind_id


def log_hand_state(gs: GameLoopState, state: dict) -> None:
    """Log hand composition and first-hand-of-blind context."""
    hand_cards = state.get("hand", {}).get("cards", [])
    hand_id = tuple(c.get("label", "") for c in hand_cards)
    if hand_id == gs.last_logged_hand:
        return

    hand_str = ", ".join(fmt_card(c) for c in hand_cards)
    if gs.prev_hand_labels:
        remaining = list(gs.prev_hand_labels)
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
    gs.prev_hand_labels = [c.get("label", "") for c in hand_cards]
    gs.last_logged_hand = hand_id

    # First hand of a new blind — log context once
    if not gs.hand_context_logged and gs.current_blind_name:
        jokers_now = state.get("jokers", {}).get("cards", [])
        hand_levels_now = state.get("hands", {})
        rnd = state.get("round", {})
        from balatro_bot.domain.scoring.search import best_hand as _bh
        jlimit = state.get("jokers", {}).get("limit", 5)
        bh = _bh(hand_cards, hand_levels_now, jokers=jokers_now, joker_limit=jlimit)
        best_score = bh.total if bh else 0
        best_name = bh.hand_name if bh else "?"
        blind_need = gs.current_blind_target or 0
        can_win = best_score >= blind_need if blind_need else False
        log.info(
            "[HAND] Best: %s for %s | Blind: %s needs %s | Hands: %d | %s",
            best_name, f"{best_score:,}", gs.current_blind_name,
            f"{blind_need:,}" if isinstance(blind_need, int) else blind_need,
            rnd.get("hands_left", 0),
            "CAN WIN" if can_win else "NEED MORE",
        )
        _stream_log.info(
            "Best: %s for %s | Hands: %d | %s",
            best_name, f"{best_score:,}",
            rnd.get("hands_left", 0),
            "CAN WIN" if can_win else "NEED MORE",
        )
        gs.hand_context_logged = True


# ---------------------------------------------------------------------------
# Action logging
# ---------------------------------------------------------------------------

def log_action(
    gs: GameLoopState, game_state: str, method: str, params: dict | None,
    action: object, card_detail: str, state: dict,
) -> None:
    """Log action to both main and stream loggers."""
    log.info(
        "[#%d] %s -> %s(%s)%s | %s",
        gs.actions_taken, game_state, method, params or "",
        card_detail, getattr(action, "reason", ""),
    )

    reason = getattr(action, "reason", "")
    if method == "play" and card_detail:
        if reason.startswith("milk:") or reason.startswith("plan milk:"):
            _stream_log.info("Milk: %s", reason.split(":", 1)[1].strip())
        elif not getattr(action, "total", 0):
            _stream_log.info("Play: %s (%s)", card_detail, reason)
        else:
            score_val = action.total
            hand_name_str = getattr(action, "hand_name", "") or "?"
            score_str = f"{score_val:,}"
            chips_remaining = state.get("round", {}).get("chips", 0)
            blind_need = gs.current_blind_target if isinstance(gs.current_blind_target, int) else 0
            remaining = blind_need - chips_remaining if blind_need else 0
            _stream_log.info(
                "Play %s \u2192 %s / %s needed",
                hand_name_str, score_str,
                f"{remaining:,}" if remaining > 0 else "0",
            )
    elif method == "discard" and card_detail:
        chase_m = re.search(r"chase (\w[\w ]*?) \((\d+)%", reason) if reason else None
        if chase_m:
            _stream_log.info(
                "Discard \u2014 chasing %s (%s%%)",
                chase_m.group(1), chase_m.group(2),
            )
        else:
            _stream_log.info("Discard")
    elif method == "buy":
        _stream_log.info("Bought: %s", reason.split("(")[0].strip() if reason else "?")
    elif method == "next_round":
        _stream_log.info("")


# ---------------------------------------------------------------------------
# Play snapshot + joker changes + game over
# ---------------------------------------------------------------------------

def build_play_snapshot(state: dict, params: dict, action: object) -> dict:
    """Build the pre-play snapshot dict for scoring diagnostics."""
    from balatro_bot.bot import find_current_blind

    hand_cards_snap = state.get("hand", {}).get("cards", [])
    play_indices = set(params.get("cards", []))

    # Detect The Flint and halve hand levels for accurate scoring
    snap_hand_levels = state.get("hands", {})
    if not state.get("_boss_disabled", False):
        blind_info = find_current_blind(state)
        if blind_info and blind_info[0] == "The Flint":
            from balatro_bot.domain.scoring.base import flint_halve_hand_levels
            snap_hand_levels = flint_halve_hand_levels(snap_hand_levels)

    blind_info = find_current_blind(state)
    snap_blind_name = blind_info[0] if blind_info else ""

    return {
        "played": [hand_cards_snap[i] for i in sorted(params.get("cards", [])) if i < len(hand_cards_snap)],
        "held": [c for j, c in enumerate(hand_cards_snap) if j not in play_indices],
        "jokers": state.get("jokers", {}).get("cards", []),
        "hand_levels": snap_hand_levels,
        "money": state.get("money", 0),
        "discards_left": state.get("round", {}).get("discards_left", 0),
        # Game does NOT decrement hands_left before scoring — Acrobat/Dusk
        # check the pre-play value. Use the raw state value.
        "hands_left": state.get("round", {}).get("hands_left", 1),
        "joker_limit": state.get("jokers", {}).get("limit", 5),
        "hand_name": getattr(action, "hand_name", ""),
        "ancient_suit": state.get("round", {}).get("ancient_suit"),
        "deck_count": state.get("cards", {}).get("count", 0),
        "deck_cards": state.get("cards", {}).get("cards", []),
        "blind_name": snap_blind_name,
        "ante": state.get("ante_num", 1),
        "_boss_disabled": state.get("_boss_disabled", False),
        "_ox_most_played": state.get("round", {}).get("most_played_poker_hand"),
    }


def detect_joker_changes(gs: GameLoopState, state: dict) -> None:
    """Detect joker roster changes, log strategy shift, handle Luchador sell."""
    cur_joker_keys = {joker_key(j) for j in state.get("jokers", {}).get("cards", [])}
    if cur_joker_keys != gs.prev_joker_keys and gs.prev_joker_keys:
        from balatro_bot.strategy import compute_strategy as _cs
        new_strat = _cs(state.get("jokers", {}).get("cards", []), state.get("hands", {}))
        log.info("[STRAT] %s", new_strat.describes())
        # Luchador sold during a boss blind → boss effect disabled
        if "j_luchador" in gs.prev_joker_keys and "j_luchador" not in cur_joker_keys:
            from balatro_bot.domain.policy.playing import BOSS_BLINDS
            if gs.current_blind_name in BOSS_BLINDS:
                state["_boss_disabled"] = True
                log.info("[BOSS] Luchador sold — %s effect disabled", gs.current_blind_name)
    gs.prev_joker_keys = cur_joker_keys


def log_game_over(gs: GameLoopState, state: dict) -> None:
    """Log game over summary and handle win capture."""
    from balatro_bot.bot import _win_handler

    # Capture chips from the final state
    final_chips = state.get("round", {}).get("chips", 0)
    if final_chips > gs.max_chips_this_blind:
        gs.max_chips_this_blind = final_chips

    # Log final round summary if died on a blind
    log_blind_result(gs, "LOST")

    ante = state.get("ante_num", "?")
    round_num = state.get("round_num", "?")
    seed = state.get("seed", "?")
    if gs.actually_won:
        log.info(
            "Game over: VICTORY (died in endless) | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, gs.actions_taken,
        )
    elif state.get("won", False):
        log.info(
            "Game over: DEFEAT (state.won=true but never reached ante 9) | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, gs.actions_taken,
        )
    else:
        log.info(
            "Game over: DEFEAT | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, gs.actions_taken,
        )
        _stream_log.info("")
        _stream_log.info("GAME OVER at Ante %s", ante)
    joker_names = [j.get("label", "?") for j in state.get("jokers", {}).get("cards", [])]
    log.info(
        "Summary: $%d | jokers: [%s] | hands=%d discards=%d",
        state.get("money", 0),
        ", ".join(joker_names) if joker_names else "none",
        gs.total_hands_played, gs.total_discards_used,
    )

    if _win_handler:
        if gs.actually_won:
            _win_handler.flush_win(seed)
        else:
            _win_handler.reset()


# ---------------------------------------------------------------------------
# Scoring diagnostics (the 270-line _log_played_hand)
# ---------------------------------------------------------------------------

def log_played_hand(snapshot: dict | None, pre_chips: int, new_state: dict) -> None:
    """Log a detailed scoring breakdown for a hand that was just played."""
    if not snapshot or not _scoring_log.handlers:
        return
    try:
        from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
        from balatro_bot.domain.scoring.estimate import score_hand_detailed

        played = snapshot["played"]
        hand_name = snapshot["hand_name"]
        if not hand_name:
            hand_name = classify_hand(played)

        joker_keys = {joker_key(j) for j in snapshot["jokers"]}
        has_splash = "j_splash" in joker_keys
        four_fingers = "j_four_fingers" in joker_keys
        smeared = "j_smeared" in joker_keys
        shortcut = "j_shortcut" in joker_keys
        scoring = played if has_splash else _scoring_cards_for(hand_name, played, four_fingers=four_fingers, smeared=smeared, shortcut=shortcut)

        # Apply boss blind level adjustments for scoring estimate
        # NOTE: The Flint halving is already applied in the snapshot,
        # so we must NOT halve again here — that caused double-halving.
        hand_levels = snapshot["hand_levels"]
        blind_name = snapshot.get("blind_name", "")
        boss_disabled = snapshot.get("_boss_disabled", False)
        if blind_name == "The Arm" and not boss_disabled:
            from balatro_bot.domain.scoring.base import arm_reduce_hand_levels
            hand_levels = arm_reduce_hand_levels(hand_levels)

        # Boss blind hand-type restrictions — zero estimate when hand is invalid
        blind_zeroed = False
        if not boss_disabled:
            if blind_name == "The Mouth":
                current_played = False
                other_played = False
                for ht, data in hand_levels.items():
                    if isinstance(data, dict) and data.get("played_this_round", 0) > 0:
                        if ht == hand_name:
                            current_played = True
                        else:
                            other_played = True
                if other_played and not current_played:
                    blind_zeroed = True
            elif blind_name == "The Eye":
                for ht, data in hand_levels.items():
                    if ht == hand_name and isinstance(data, dict) and data.get("played_this_round", 0) > 0:
                        blind_zeroed = True
                        break
            elif blind_name == "The Psychic" and len(played) < 5:
                blind_zeroed = True

        # The Ox locks most-played hand at blind start
        _ox_mp = snapshot.get("_ox_most_played")

        detail = score_hand_detailed(
            hand_name, scoring,
            hand_levels=hand_levels,
            jokers=snapshot["jokers"],
            played_cards=played,
            held_cards=snapshot["held"],
            money=snapshot["money"],
            discards_left=snapshot["discards_left"],
            hands_left=snapshot["hands_left"],
            joker_limit=snapshot["joker_limit"],
            ancient_suit=snapshot.get("ancient_suit"),
            deck_count=snapshot.get("deck_count", 0),
            deck_cards=snapshot.get("deck_cards"),
            blind_name="" if boss_disabled else blind_name,
            ox_most_played=_ox_mp,
        )

        if blind_zeroed:
            detail["total"] = 0

        post_chips = new_state.get("round", {}).get("chips", 0)
        actual_chips = post_chips - pre_chips
        actual_reliable = actual_chips > 0
        cards_str = ", ".join(fmt_card(c) for c in played)

        joker_parts = []
        for entry in detail["joker_contributions"]:
            label, dc, dm = entry[0], entry[1], entry[2]
            xm = entry[3] if len(entry) > 3 else 1.0
            parts = []
            if dc:
                parts.append(f"+{dc:.0f}c")
            # Show xmult as multiplier if it's clearly multiplicative (ratio > 1.1 or < 0.9)
            if xm > 1.01 or xm < 0.99:
                parts.append(f"x{xm:.2f}")
            elif dm:
                parts.append(f"+{dm:.1f}m")
            if parts:
                joker_parts.append(f"{label}({', '.join(parts)})")
        joker_str = ", ".join(joker_parts) if joker_parts else "none"

        mismatch = ""
        is_mismatch = actual_reliable and detail["total"] != actual_chips
        # Probability-based effects make scoring non-deterministic — the bot
        # uses expected values but the game rolls dice.  Tag these as noise.
        if is_mismatch:
            noise_sources = []
            if "j_misprint" in joker_keys:
                noise_sources.append("misprint")
            if "j_bloodstone" in joker_keys:
                noise_sources.append("bloodstone")
            # Space Joker: 1/8 chance to upgrade hand level — unpredictable
            if "j_space" in joker_keys:
                noise_sources.append("space")
            # Lucky cards: 1/5 chance for +20 mult per scored Lucky card
            if any(
                isinstance(c.get("modifier", {}), dict)
                and c.get("modifier", {}).get("enhancement") == "LUCKY"
                for c in scoring
            ):
                noise_sources.append("lucky")
            # Hook's boss effect (discard 2 cards) fires BEFORE On Played
            # jokers.  Modelled: Green Joker, Ramen, Yorick.
            # Castle/Hit the Road gain from Hook's random discards — inherently
            # noisy since we can't predict which cards Hook picks.
            if blind_name == "The Hook":
                hook_noisy = {"j_castle", "j_hit_the_road",
                              "j_ride_the_bus",
                              "j_runner", "j_square", "j_trousers", "j_wee",
                              "j_lucky_cat", "j_obelisk"}
                for jk in joker_keys & hook_noisy:
                    noise_sources.append(f"hook+{jk.removeprefix('j_')}")
                # Hook removes held cards before scoring — held-card effects
                # (Steel, Baron, Smiley Face, Blackboard) see a reduced hand
                # that the bot can't predict.
                held = snapshot.get("held", [])
                _hmod = lambda c: c.get("modifier", {}) if isinstance(c.get("modifier", {}), dict) else {}
                if any(_hmod(c).get("enhancement") == "STEEL" for c in held):
                    noise_sources.append("hook+steel")
                if "j_baron" in joker_keys and any(c.get("value", {}).get("rank") == "K" for c in held):
                    noise_sources.append("hook+baron")
                if "j_smiley" in joker_keys:
                    noise_sources.append("hook+smiley")
                if "j_blackboard" in joker_keys:
                    noise_sources.append("hook+blackboard")
            if noise_sources:
                diff = actual_chips - detail["total"]
                mismatch = f" MISMATCH_NOISE(diff={diff:+d}, {'+'.join(noise_sources)})"
                is_mismatch = False
        if is_mismatch:
            diff = actual_chips - detail["total"]
            if abs(diff) <= 1:
                mismatch = f" MISMATCH_NOISE(diff={diff:+d}, rounding)"
                is_mismatch = False
            else:
                mismatch = f" MISMATCH(diff={diff:+d})"
        if not mismatch and not actual_reliable:
            mismatch = " (actual unreliable)"

        scoring_str = ", ".join(fmt_card(c) for c in scoring)

        def _mod(c: dict) -> dict:
            m = c.get("modifier", {})
            return m if isinstance(m, dict) else {}

        enhs_str = ",".join(sorted(filter(None, {_mod(c).get("enhancement", "") for c in scoring})))
        seals_str = ",".join(sorted(filter(None, {_mod(c).get("seal", "") for c in scoring})))
        eds_str = ",".join(sorted(filter(None, {_mod(c).get("edition", "") for c in scoring})))

        _scoring_log.info(
            "%s [%s] scoring=[%s](%d) | base: %d/%d | pre-joker: %d/%.1f | jokers: [%s] | "
            "final: %d/%.1f | enhs=[%s] seals=[%s] eds=[%s] | "
            "blind=%s ante=%d hands_left=%d | est=%d actual=%d%s",
            detail["hand_name"], cards_str, scoring_str, len(scoring),
            detail["base_chips"], detail["base_mult"],
            detail["pre_joker_chips"], detail["pre_joker_mult"],
            joker_str,
            detail["post_joker_chips"], detail["post_joker_mult"],
            enhs_str, seals_str, eds_str,
            snapshot.get("blind_name", ""), snapshot.get("ante", 0), snapshot.get("hands_left", 0),
            detail["total"], actual_chips,
            mismatch,
        )

        # On mismatch, dump per-card scoring breakdown and raw card data
        if is_mismatch:
            from balatro_bot.cards import card_chip_value, card_mult_value, card_xmult_value, _modifier
            from balatro_bot.joker_effects import parse_effect_value, retrigger_count, ScoreContext

            # Dump raw hand level data from API for this hand type
            hand_level_data = snapshot["hand_levels"].get(hand_name, {})
            _scoring_log.info("  hand_level[%s] = %s", hand_name, hand_level_data)

            # Score context intermediate states
            _scoring_log.info(
                "  scoring_ctx: base=%d/%d pre_joker=%d/%.1f post_joker=%d/%.1f total=%d",
                detail["base_chips"], detail["base_mult"],
                detail["pre_joker_chips"], detail["pre_joker_mult"],
                detail["post_joker_chips"], detail["post_joker_mult"],
                detail["total"],
            )

            # Build minimal context for retrigger calculation
            ctx_for_retrigger = ScoreContext(
                chips=0, mult=0.0, hand_name=hand_name,
                scoring_cards=scoring, played_cards=played,
                held_cards=snapshot["held"], hand_levels=snapshot["hand_levels"],
                jokers=snapshot["jokers"], money=0, discards_left=0, hands_left=1,
                joker_limit=snapshot.get("joker_limit", 5),
                pareidolia="j_pareidolia" in joker_keys,
                smeared="j_smeared" in joker_keys,
            )

            for i, c in enumerate(scoring):
                mod = _modifier(c)
                if isinstance(c, Card):
                    rank = c.value.rank or "?"
                    suit = c.value.suit or "?"
                    enh = c.modifier.enhancement or ""
                    edition = c.modifier.edition or ""
                    seal = c.modifier.seal or ""
                    debuff = c.state.debuff
                    perma = c.value.perma_bonus
                else:
                    rank = c.get("value", {}).get("rank", "?")
                    suit = c.get("value", {}).get("suit", "?")
                    enh = mod.get("enhancement", "")
                    edition = mod.get("edition", "")
                    seal = mod.get("seal", "")
                    debuff = c.get("state", {})
                    if isinstance(debuff, dict):
                        debuff = debuff.get("debuff", False)
                    else:
                        debuff = False
                    perma = c.get("value", {}).get("perma_bonus", 0) or 0
                chips = card_chip_value(c)
                mult = card_mult_value(c)
                xmult = card_xmult_value(c)
                triggers = retrigger_count(c, ctx_for_retrigger)
                _scoring_log.info(
                    "  card[%d] %s %s (SCORING) | chips=%d mult=%.1f xmult=%.2f triggers=%d | "
                    "enh=%s ed=%s seal=%s debuff=%s perma=%d | raw_mod=%s",
                    i, rank, suit,
                    chips, mult, xmult, triggers,
                    enh or "-", edition or "-", seal or "-", debuff, perma,
                    mod,
                )
            # Dump all held cards (Baron checks Kings, Shoot the Moon checks Queens, etc.)
            for i, c in enumerate(snapshot["held"]):
                if isinstance(c, Card):
                    rank_h = c.value.rank or "?"
                    suit_h = c.value.suit or "?"
                    enh_h = c.modifier.enhancement or ""
                else:
                    mod_h = _modifier(c)
                    rank_h = c.get("value", {}).get("rank", "?")
                    suit_h = c.get("value", {}).get("suit", "?")
                    enh_h = mod_h.get("enhancement", "")
                _scoring_log.info("  held[%d] %s %s | enh=%s", i, rank_h, suit_h, enh_h or "-")
            # Dump raw joker ability data + parsed values so we can compare
            for i, j in enumerate(snapshot["jokers"]):
                ability = j.get("value", {}).get("ability", {})
                effect = j.get("value", {}).get("effect", "") or ""
                if not isinstance(effect, str):
                    effect = ""
                parsed = parse_effect_value(effect)
                rarity = j.get("value", {}).get("rarity", "?")
                jmod = j.get("modifier", {})
                if not isinstance(jmod, dict):
                    jmod = {}
                jed = jmod.get("edition", "")
                jed_parts = []
                if jed:
                    jed_parts.append(jed)
                    if jmod.get("edition_mult"):
                        jed_parts.append(f"mult={jmod['edition_mult']}")
                    if jmod.get("edition_chips"):
                        jed_parts.append(f"chips={jmod['edition_chips']}")
                    if jmod.get("edition_x_mult"):
                        jed_parts.append(f"x_mult={jmod['edition_x_mult']}")
                jed_str = " ".join(jed_parts) if jed_parts else "-"
                _scoring_log.info(
                    "  joker[%d] %s | rarity=%s ed=%s | ability=%s | parsed=%s | effect=%s",
                    i, joker_key(j) or "?", rarity, jed_str, ability, parsed,
                    effect[:120] if effect else "-",
                )
    except Exception as e:
        _scoring_log.warning("scoring log error: %s", e)
