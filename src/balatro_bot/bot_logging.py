"""Game log text: state transitions, actions, scoring diagnostics. These functions write
log lines only; the game's record is kept by ``game_recorder``."""

from __future__ import annotations

import logging
import re

from balatro_bot.bot_format import fmt_card
from balatro_bot.cards import joker_key
from balatro_bot.domain.models.card import Card

log = logging.getLogger("balatro_bot")
_stream_log = logging.getLogger("balatro_stream")
_scoring_log = logging.getLogger("balatro_scoring")


def _target_text(target: int | str | None) -> str | None:
    return f"{target:,}" if isinstance(target, int) else target


# ---------------------------------------------------------------------------
# Round / blind result
# ---------------------------------------------------------------------------

def log_blind_result(
    blind_name: str, scored: int, target: int | str | None, result: str,
    hands_used: int, discards_used: int,
) -> None:
    """Round summary for a finished blind."""
    log.info(
        "[ROUND] %s: scored %s / needed %s — %s | %d hands, %d discards",
        blind_name, f"{scored:,}", _target_text(target), result, hands_used, discards_used,
    )
    if result == "WON":
        _stream_log.info(
            "%s WON — %s / %s | %d hands, %d discards",
            blind_name, f"{scored:,}", _target_text(target), hands_used, discards_used,
        )


# ---------------------------------------------------------------------------
# Per-state logging helpers
# ---------------------------------------------------------------------------

def log_ante(state: dict, deck_snapshot: str, strategy: str) -> None:
    """Roster, deck, strategy and hand levels at the start of an ante."""
    from balatro_bot.joker_effects import parse_effect_value

    ante_num = state.get("ante_num")
    joker_cards = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})
    money = state.get("money", 0)

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
    log.info("[ANTE %s] Deck %s", ante_num, deck_snapshot)
    log.info("[ANTE %s] Strategy: %s", ante_num, strategy)

    leveled = [
        f"{ht}(lv{data['level']})" for ht, data in hand_levels.items()
        if hasattr(data, "get") and data.get("level", 1) > 1
    ]
    if leveled:
        log.info("[ANTE %s] Money: $%d | Levels: %s", ante_num, money, ", ".join(leveled))
    else:
        log.info("[ANTE %s] Money: $%d", ante_num, money)

    _stream_log.info("")
    _stream_log.info("=== Ante %s / 8 ===", ante_num)
    joker_names = [j.get("label", "?") for j in joker_cards]
    _stream_log.info("Jokers: %s", ", ".join(joker_names) if joker_names else "(none)")
    _stream_log.info("Money: $%d", money)


def log_shop(state: dict) -> None:
    """The shop's stock."""
    def priced(cards):
        return [f"{c.get('label','?')}(${c.get('cost',{}).get('buy','?')})" for c in cards]

    shop_cards = state.get("shop", {}).get("cards", [])
    jokers_avail = priced(c for c in shop_cards if c.get("set") == "JOKER")
    consumables_avail = priced(c for c in shop_cards if c.get("set") not in ("JOKER",))
    packs_avail = priced(state.get("packs", {}).get("cards", []))
    vouchers_avail = priced(state.get("vouchers", {}).get("cards", []))
    parts = []
    if jokers_avail:
        parts.append("jokers: " + ", ".join(jokers_avail))
    if consumables_avail:
        parts.append("consumables: " + ", ".join(consumables_avail))
    if packs_avail:
        parts.append("packs: " + ", ".join(packs_avail))
    if vouchers_avail:
        parts.append("vouchers: " + ", ".join(vouchers_avail))
    log.info("Shop ($%d): %s", state.get("money", 0), " | ".join(parts) if parts else "(empty)")


def log_blind(name: str, score: int | str) -> None:
    """A blind coming up."""
    log.info("Blind: %s (need %s chips)", name, score)
    _stream_log.info("")
    _stream_log.info("--- %s — need %s chips ---", name, f"{score:,}" if isinstance(score, int) else score)


def log_hand(hand_cards: list, prev_labels: list) -> None:
    """The hand held, with the cards drawn since ``prev_labels`` and any debuffs."""
    hand_str = ", ".join(fmt_card(c) for c in hand_cards)
    drew_str = ""
    if prev_labels:
        remaining = list(prev_labels)
        drew = []
        for c in hand_cards:
            lbl = c.get("label", "")
            if lbl in remaining:
                remaining.remove(lbl)
            else:
                drew.append(fmt_card(c))
        drew_str = f" | drew: [{', '.join(drew)}]" if drew else ""
    debuffed = [fmt_card(c) for c in hand_cards
                if isinstance(c.get("state", {}), dict) and c["state"].get("debuff")]
    debuff_str = f" | DEBUFFED: [{', '.join(debuffed)}]" if debuffed else ""
    log.info("Hand: [%s]%s%s", hand_str, drew_str, debuff_str)


def log_hand_context(state: dict, blind_name: str, blind_target: int | str | None) -> None:
    """The best hand on offer at the first hand of a blind, against what the blind needs."""
    from balatro_bot.domain.scoring.search import best_hand as _bh
    from balatro_bot.infrastructure.state_adapter import adapt_state

    rnd = state.get("round", {})
    # Typed cards and jokers: hand search caches its per-card and per-lineup facts on them.
    snap = adapt_state(state)
    bh = _bh(snap.hand_cards, snap.hand_levels, jokers=snap.jokers, joker_limit=snap.joker_limit)
    best_score = bh.total if bh else 0
    best_name = bh.hand_name if bh else "?"
    blind_need = blind_target or 0
    can_win = best_score >= blind_need if blind_need else False
    log.info(
        "[HAND] Best: %s for %s | Blind: %s needs %s | Hands: %d | %s",
        best_name, f"{best_score:,}", blind_name,
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


# ---------------------------------------------------------------------------
# Action logging
# ---------------------------------------------------------------------------

def log_action(
    seq: int, game_state: str, method: str, params: dict | None,
    action: object, card_detail: str, state: dict, blind_target: int | str | None,
) -> None:
    """Log action to both main and stream loggers."""
    log.info(
        "[#%d] %s -> %s(%s)%s | %s",
        seq, game_state, method, params or "",
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
            blind_need = blind_target if isinstance(blind_target, int) else 0
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
# Play snapshot + game over
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


def build_play_snapshot(state: dict, params: dict, action: object) -> dict:
    """Build the pre-play snapshot dict for scoring diagnostics."""
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
        "idol_rank": (state.get("round", {}).get("idol_card") or {}).get("rank"),
        "idol_suit": (state.get("round", {}).get("idol_card") or {}).get("suit"),
        "deck_count": state.get("cards", {}).get("count", 0),
        "blind_name": snap_blind_name,
        "ante": state.get("ante_num", 1),
        "_boss_disabled": state.get("_boss_disabled", False),
        "_ox_most_played": state.get("round", {}).get("most_played_poker_hand"),
    }


def log_game_over(state: dict, *, won: bool, actions: int, hands: int, discards: int) -> None:
    """Game over summary."""
    ante = state.get("ante_num", "?")
    round_num = state.get("round_num", "?")
    seed = state.get("seed", "?")
    if won:
        log.info(
            "Game over: VICTORY (died in endless) | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, actions,
        )
    elif state.get("won", False):
        log.info(
            "Game over: DEFEAT (state.won=true but never reached ante 9) | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, actions,
        )
    else:
        log.info(
            "Game over: DEFEAT | seed=%s ante=%s round=%s | %d actions taken",
            seed, ante, round_num, actions,
        )
        _stream_log.info("")
        _stream_log.info("GAME OVER at Ante %s", ante)
    joker_names = [j.get("label", "?") for j in state.get("jokers", {}).get("cards", [])]
    log.info(
        "Summary: $%d | jokers: [%s] | hands=%d discards=%d",
        state.get("money", 0),
        ", ".join(joker_names) if joker_names else "none",
        hands, discards,
    )


# ---------------------------------------------------------------------------
# Scoring diagnostics
# ---------------------------------------------------------------------------

def compute_played_hand_detail(snapshot: dict | None) -> dict | None:
    """Compute the scored-hand detail dict for a just-played hand.

    Returns None when the snapshot is missing. Safe to call regardless of
    logging configuration — used by both log_played_hand and the dashboard
    hand_scores payload.
    """
    if not snapshot:
        return None
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

    hand_levels = snapshot["hand_levels"]
    blind_name = snapshot.get("blind_name", "")
    boss_disabled = snapshot.get("_boss_disabled", False)
    if blind_name == "The Arm" and not boss_disabled:
        from balatro_bot.domain.scoring.base import arm_reduce_hand_levels
        hand_levels = arm_reduce_hand_levels(hand_levels)

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
        idol_rank=snapshot.get("idol_rank"),
        idol_suit=snapshot.get("idol_suit"),
        deck_count=snapshot.get("deck_count", 0),
        blind_name="" if boss_disabled else blind_name,
        ox_most_played=_ox_mp,
    )

    if blind_zeroed:
        detail["total"] = 0

    detail["_hand_name"] = hand_name
    detail["_scoring"] = scoring
    detail["_joker_keys"] = joker_keys
    return detail


def serialize_joker_contributions(detail: dict | None) -> list[dict] | None:
    """Convert detail['joker_contributions'] tuples into JSON-friendly dicts."""
    if not detail:
        return None
    entries = detail.get("joker_contributions") or []
    out = []
    for entry in entries:
        label = entry[0]
        dc = entry[1] if len(entry) > 1 else 0
        dm = entry[2] if len(entry) > 2 else 0
        xm = entry[3] if len(entry) > 3 else 1.0
        out.append({
            "name": label,
            "chips": float(dc) if dc else 0.0,
            "mult": float(dm) if dm else 0.0,
            "xmult": float(xm) if xm else 1.0,
        })
    return out


def log_played_hand(snapshot: dict | None, pre_chips: int, new_state: dict) -> None:
    """Log a detailed scoring breakdown for a hand that was just played."""
    if not snapshot or not _scoring_log.handlers:
        return
    try:
        detail = compute_played_hand_detail(snapshot)
        if detail is None:
            return
        played = snapshot["played"]
        hand_name = detail["_hand_name"]
        scoring = detail["_scoring"]
        joker_keys = detail["_joker_keys"]
        blind_name = snapshot.get("blind_name", "")

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
            from balatro_bot.joker_effects import parse_effect_value

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
                triggers = detail["card_triggers"][i]
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


# ---------------------------------------------------------------------------
# Decision records (shop / pack choices for the dashboard)
# ---------------------------------------------------------------------------

def shop_offer_items(state: dict) -> list[dict]:
    """Shop cards, vouchers and packs on offer, in the game_shop_offers item format."""
    shop_cards = state.get("shop", {}).get("cards", []) or []
    vouchers = state.get("vouchers", {}).get("cards", []) or []
    packs = state.get("packs", {}).get("cards", []) or []
    items = []
    for c in shop_cards:
        items.append({
            "slot": "shop",
            "set": c.get("set"),
            "key": c.get("key"),
            "label": c.get("label"),
            "cost": (c.get("cost") or {}).get("buy"),
            "rarity": c.get("rarity"),
            "edition": (c.get("edition") or None) if c.get("edition") else None,
        })
    for v in vouchers:
        items.append({
            "slot": "voucher",
            "set": "VOUCHER",
            "key": v.get("key"),
            "label": v.get("label"),
            "cost": (v.get("cost") or {}).get("buy"),
        })
    for p in packs:
        items.append({
            "slot": "pack",
            "set": "BOOSTER",
            "key": p.get("key"),
            "label": p.get("label"),
            "cost": (p.get("cost") or {}).get("buy"),
        })
    return items


def pack_offer_items(state: dict) -> list[dict]:
    """Cards in the open pack, in the game_pack_offers item format."""
    pack_cards = state.get("pack", {}).get("cards", []) or []
    items = []
    for c in pack_cards:
        items.append({
            "key": c.get("key"),
            "label": c.get("label"),
            "set": c.get("set"),
            "rank": (c.get("value") or {}).get("rank"),
            "suit": (c.get("value") or {}).get("suit"),
            "enhancement": (c.get("modifier") or {}).get("enhancement") if isinstance(c.get("modifier"), dict) else None,
            "edition": (c.get("edition") or None) if c.get("edition") else None,
            "seal": (c.get("modifier") or {}).get("seal") if isinstance(c.get("modifier"), dict) else None,
        })
    return items


def deck_composition(cards: list[dict]) -> dict:
    """Rank / suit / enhancement / seal counts for a list of playing cards."""
    comp: dict = {"total": len(cards), "ranks": {}, "suits": {}, "enhancements": {}, "seals": {}}
    for c in cards:
        v = c.get("value") or {}
        m = c.get("modifier") if isinstance(c.get("modifier"), dict) else {}
        rank = v.get("rank") or "STONE"
        suit = v.get("suit") or "NONE"
        enh = (m or {}).get("enhancement") or "NONE"
        seal = (m or {}).get("seal") or "NONE"
        comp["ranks"][rank] = comp["ranks"].get(rank, 0) + 1
        comp["suits"][suit] = comp["suits"].get(suit, 0) + 1
        comp["enhancements"][enh] = comp["enhancements"].get(enh, 0) + 1
        comp["seals"][seal] = comp["seals"].get(seal, 0) + 1
    return comp
