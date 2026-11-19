"""The record of one game: what the bot saw and did, and the dashboard's copy of it."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from balatro_bot.bot_format import format_card_detail, format_deck_snapshot
from balatro_bot.bot_logging import (
    build_play_snapshot, compute_played_hand_detail, deck_composition, log_action, log_ante,
    log_blind, log_blind_result, log_game_over, log_hand, log_hand_context, log_played_hand,
    log_shop, pack_offer_items, serialize_joker_contributions, shop_offer_items,
)
from balatro_bot.cards import joker_key
from balatro_bot.dispatcher import PACK_STATES
from balatro_bot.domain.policy.exploration import explorer
from balatro_bot.domain.policy.playing import BOSS_BLINDS

log = logging.getLogger("balatro_bot")
_stream_log = logging.getLogger("balatro_stream")
_scoring_log = logging.getLogger("balatro_scoring")

# States the game passes through on its own; the bot only waits in them.
PASSING_STATES = frozenset({"HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "SPLASH", "TUTORIAL"})


@dataclass
class GameLoopState:
    actions_taken: int = 0
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
    consumables_bought: list = field(default_factory=list)  # [{name, consumable_type, count}]
    action_log: list = field(default_factory=list)
    explored_actions: list = field(default_factory=list)  # positions in the replay's action list
    ante_snapshots: list = field(default_factory=list)
    hand_scores: list = field(default_factory=list)
    shop_events: list = field(default_factory=list)
    hands_scored: int = 0
    packs_opened: int = 0
    in_pack_state: bool = False
    shop_offers: list = field(default_factory=list)
    pack_offers: list = field(default_factory=list)
    tags_seen: list = field(default_factory=list)
    blind_decisions: list = field(default_factory=list)
    decisions: list = field(default_factory=list)  # shop/pack choices
    shop_visit_seq: int = 0
    last_shop_signature: str | None = None  # a changed roster in the same shop is a reroll
    pack_seq: int = 0
    current_pack_label: str | None = None
    last_tag_keys: list = field(default_factory=list)
    death_info: dict | None = None


@dataclass
class _Pending:
    """An action on its way to the game, with what it acted on read before the game
    changes it."""

    game_state: str
    method: str
    params: dict | None
    action: object
    decision: dict | None
    decision_state: dict | None
    money: int
    chips: int
    hand: list | None
    play_snapshot: dict | None
    buying_joker: str | None = None
    buying_consumable: dict | None = None
    buying_pack: str | None = None
    picking_label: str | None = None
    picking_set: str | None = None
    selling_joker: str | None = None
    using_consumable: str | None = None


def _label_at(cards: list, idx: int) -> str | None:
    return cards[idx].get("label", "?") if idx < len(cards) else None


def build_decision_state(gs: GameLoopState, state: dict) -> dict:
    """Game state at a shop or pack decision, close to what the game shows.

    Kept raw (keys, effect text, counts) rather than the bot's derived
    features so records stay usable when valuation code changes.
    """
    from balatro_bot.cards import _modifier
    from balatro_bot.joker_effects import parse_effect_value

    joker_info = state.get("jokers", {})
    jokers = []
    for j in joker_info.get("cards", []) or []:
        effect = (j.get("value") or {}).get("effect", "") or ""
        parsed = parse_effect_value(effect) if effect else {}
        mod = _modifier(j)
        jokers.append({
            "key": joker_key(j),
            "edition": mod.get("edition"),
            "stickers": [k for k in ("eternal", "perishable", "rental") if mod.get(k)] or None,
            "sell": (j.get("cost") or {}).get("sell"),
            "effect": effect or None,
            "chips": parsed.get("chips"),
            "mult": parsed.get("mult"),
            "xmult": parsed.get("xmult"),
        })
    cons_info = state.get("consumables", {})
    used_vouchers = state.get("used_vouchers", {})
    hand_levels = {
        ht: d.get("level") for ht, d in (state.get("hands") or {}).items()
        if hasattr(d, "get") and (d.get("level") or 1) > 1
    }
    deck_cards = (state.get("cards", {}).get("cards", []) or []) + (state.get("hand", {}).get("cards", []) or [])
    game_state = state.get("state", "")
    return {
        "money": state.get("money"),
        "ante": state.get("ante_num"),
        "round": state.get("round_num"),
        "shop_visit_seq": gs.shop_visit_seq if game_state == "SHOP" else None,
        "pack_seq": gs.pack_seq if game_state != "SHOP" else None,
        "jokers": jokers,
        "joker_limit": joker_info.get("limit"),
        "consumables": [c.get("key") for c in cons_info.get("cards", []) or []],
        "consumable_limit": cons_info.get("limit"),
        "vouchers": sorted(used_vouchers.keys()) if isinstance(used_vouchers, dict) else [],
        "hand_levels": hand_levels,
        "deck": deck_composition(deck_cards),
        "blinds": [
            {"name": b.get("name"), "status": b.get("status")}
            for b in (state.get("blinds") or {}).values() if isinstance(b, dict)
        ],
        "shop": shop_offer_items(state) if game_state == "SHOP" else None,
        "pack": pack_offer_items(state) if game_state != "SHOP" else None,
    }


def record_decision(
    gs: GameLoopState,
    decision: dict,
    decision_state: dict | None,
    game_state: str,
    method: str,
    params: dict | None,
    money_before: int,
    money_after: int | None,
    error: str | None = None,
) -> None:
    """Append one shop/pack decision to the game's decision log."""
    detail = {
        k: v for k, v in decision.items()
        if k not in ("kind", "continuation", "chosen_idx", "policy")
    }
    if explorer().rate > 0:
        detail["explore_rate"] = explorer().rate
    gs.decisions.append({
        "seq": gs.actions_taken,
        "kind": decision.get("kind"),
        "game_state": game_state,
        "policy": decision.get("policy", "greedy"),
        "continuation": bool(decision.get("continuation")),
        "chosen_idx": decision.get("chosen_idx"),
        "action": [method, params],
        "money_before": money_before,
        "money_after": money_after,
        "error": error,
        "state": decision_state,
        "detail": detail or None,
    })


class GameRecorder:
    """Keeps one game's record from the states the bot sees and the actions it sends.

    Per tick: ``observe`` the state; for each action, ``sending`` before it goes and
    then ``acted``, ``refused`` or ``timed_out``; at the end ``finish`` and ``payload``.
    """

    def __init__(self) -> None:
        self._gs = GameLoopState()
        self._pending: _Pending | None = None

    # -- reads --------------------------------------------------------------

    @property
    def won(self) -> bool:
        """Beat the ante 8 boss. ``state.won`` is not trusted: Hieroglyph moves the ante back."""
        return self._gs.actually_won

    @property
    def explored_actions(self) -> list[int]:
        return self._gs.explored_actions

    def live_stats(self):
        """Per-run averages of the bot's play so far, or None before a round has ended."""
        from balatro_bot.domain.policy.sim_context import LiveRunStats

        gs = self._gs
        rounds_played = len(gs.round_results)
        if rounds_played <= 0:
            return None
        joker_sells = sum(
            1 for ev in gs.shop_events
            if ev.get("event_type") == "sell" and ev.get("item_type") == "joker"
        )
        antes_played = max(1, max((r.get("ante", 0) for r in gs.round_results), default=0))
        return LiveRunStats(
            avg_discards_per_round=gs.total_discards_used / rounds_played,
            avg_sells_per_ante=joker_sells / antes_played,
            avg_plays_per_round=gs.total_hands_played / rounds_played,
        )

    # -- each tick ----------------------------------------------------------

    def observe(self, state: dict) -> None:
        """Record what changed since the last state the bot saw."""
        gs = self._gs
        game_state = state.get("state", "")
        self._check_victory(state)
        if state.get("won") and not gs.won_logged:
            if not gs.actually_won:
                log.info(
                    "state.won=true at ante %s (Hieroglyph?) — not a real win until ante 9+",
                    state.get("ante_num", "?"),
                )
            gs.won_logged = True

        self._ante_transition(state)
        # The round's chips reset when it ends, so the best seen is kept as it goes.
        cur_chips = state.get("round", {}).get("chips", 0)
        if cur_chips > gs.max_chips_this_blind:
            gs.max_chips_this_blind = cur_chips
        gs.last_round = state.get("round_num")

        if game_state in PASSING_STATES:
            gs.last_logged_hand = None
            return
        if game_state != "SELECTING_HAND":
            gs.last_logged_hand = None
            gs.prev_hand_labels = []
        if game_state != "SHOP":
            gs.last_logged_shop = None
            gs.last_shop_signature = None
        if game_state == "SHOP":
            self._shop_state(state)
            self._capture_shop_offer(state)

        # Every pack opened counts, whether bought, free from a tag or from an effect.
        is_pack_state = game_state in PACK_STATES
        if is_pack_state and not gs.in_pack_state:
            gs.packs_opened += 1
            gs.current_pack_label = game_state
            self._capture_pack_offer(state)
        elif not is_pack_state and gs.in_pack_state:
            gs.current_pack_label = None
        gs.in_pack_state = is_pack_state

        self._capture_tags_diff(state)
        if game_state == "BLIND_SELECT":
            self._blind_transition(state)
        else:
            gs.last_logged_blind = None
        if game_state == "SELECTING_HAND":
            self._hand_state(state)

    def _check_victory(self, state: dict) -> None:
        gs = self._gs
        ante = state.get("ante_num", 0)
        if ante < 9 or gs.actually_won:
            return
        gs.actually_won = True
        # The winning hand's chips may only be visible in this state.
        gs.max_chips_this_blind = max(gs.max_chips_this_blind, state.get("round", {}).get("chips", 0) or 0)
        log.info(
            "VICTORY at ante %s round %s (seed=%s) — entering endless mode",
            ante, state.get("round_num", "?"), state.get("seed", "?"),
        )
        _stream_log.info("")
        _stream_log.info("*** VICTORY! ***")
        self._end_blind("WON")
        gs.current_blind_name = None

    def _capture_shop_offer(self, state: dict) -> None:
        """The shop's roster on entry and after each reroll."""
        gs = self._gs
        shop_cards = state.get("shop", {}).get("cards", []) or []
        vouchers = state.get("vouchers", {}).get("cards", []) or []
        packs = state.get("packs", {}).get("cards", []) or []
        sig = "|".join(
            [f"{c.get('set','')}:{c.get('label','')}:{c.get('cost',{}).get('buy','')}" for c in shop_cards]
            + [f"V:{v.get('label','')}" for v in vouchers]
            + [f"P:{p.get('label','')}" for p in packs]
        )
        if sig == gs.last_shop_signature:
            return
        if gs.last_shop_signature is None:
            gs.shop_visit_seq += 1
        gs.last_shop_signature = sig
        gs.shop_offers.append({
            "ante": state.get("ante_num"),
            "round": state.get("round_num"),
            "shop_visit_seq": gs.shop_visit_seq,
            "money": state.get("money"),
            "items": shop_offer_items(state),
        })

    def _capture_pack_offer(self, state: dict) -> None:
        gs = self._gs
        if not (state.get("pack", {}).get("cards", []) or []):
            return
        gs.pack_seq += 1
        gs.pack_offers.append({
            "ante": state.get("ante_num"),
            "round": state.get("round_num"),
            "pack_seq": gs.pack_seq,
            "pack_state": state.get("state"),
            "items": pack_offer_items(state),
        })

    def _capture_tags_diff(self, state: dict) -> None:
        """Each tag gained since the last state."""
        gs = self._gs
        tags = state.get("tags") or []
        cur_keys = [t.get("key", "") for t in tags]
        if cur_keys == gs.last_tag_keys:
            return
        prev_count = Counter(gs.last_tag_keys)
        for k, n in Counter(cur_keys).items():
            info = next((t for t in tags if t.get("key") == k), {})
            for _ in range(max(0, n - prev_count.get(k, 0))):
                gs.tags_seen.append({
                    "ante": state.get("ante_num"),
                    "round": state.get("round_num"),
                    "tag_key": k,
                    "tag_name": info.get("name"),
                })
        gs.last_tag_keys = cur_keys

    def _end_blind(self, result: str) -> None:
        """Close the current blind's round as WON or LOST."""
        gs = self._gs
        if not gs.current_blind_name:
            return
        hands_used = gs.total_hands_played - gs.hands_at_blind_start
        discards_used = gs.total_discards_used - gs.discards_at_blind_start
        log_blind_result(gs.current_blind_name, gs.max_chips_this_blind, gs.current_blind_target,
                         result, hands_used, discards_used)
        gs.round_results.append({
            "ante": gs.ante_at_blind_start or gs.last_ante or 0,
            "blind_name": gs.current_blind_name,
            "is_boss": gs.current_blind_name in BOSS_BLINDS,
            "scored": gs.max_chips_this_blind,
            "needed": gs.current_blind_target if isinstance(gs.current_blind_target, int) else 0,
            "won": result == "WON",
            "hands_used": hands_used,
            "discards_used": discards_used,
        })

    def _ante_transition(self, state: dict) -> None:
        """Snapshot roster, deck, strategy and hand levels when the ante changes."""
        from balatro_bot.joker_effects import parse_effect_value
        from balatro_bot.strategy import compute_strategy

        gs = self._gs
        ante_num = state.get("ante_num")
        if ante_num is None or ante_num == gs.last_ante:
            return
        joker_cards = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        for j in joker_cards:
            effect_text = j.get("value", {}).get("effect", "")
            parsed = parse_effect_value(effect_text) if effect_text else {}
            if any(parsed.get(k) for k in ("chips", "mult", "xmult")):
                gs.joker_scaling[j.get("label", "?")] = {
                    "chips": parsed.get("chips"), "mult": parsed.get("mult"), "xmult": parsed.get("xmult"),
                }
        deck_cards = state.get("cards", {}).get("cards", [])
        deck_snapshot = format_deck_snapshot(deck_cards)
        strategy = compute_strategy(joker_cards, hand_levels).describes()
        log_ante(state, deck_snapshot, strategy)

        roster = []
        for j in joker_cards:
            label = j.get("label", "?")
            scaling = gs.joker_scaling.get(label, {})
            roster.append({"name": label, "chips": scaling.get("chips"),
                           "mult": scaling.get("mult"), "xmult": scaling.get("xmult")})
        leveled = {
            ht: data["level"] for ht, data in hand_levels.items()
            if hasattr(data, "get") and data.get("level", 1) > 1
        }
        gs.ante_snapshots.append({
            "ante": ante_num,
            "money": state.get("money", 0),
            "joker_roster": roster,
            "hand_levels": leveled or None,
            "deck_size": len(deck_cards),
            "deck_snapshot": deck_snapshot,
            "strategy": strategy,
        })
        gs.last_ante = ante_num

    def _shop_state(self, state: dict) -> None:
        """The shop's stock as offer events, once per roster."""
        gs = self._gs
        shop_cards = state.get("shop", {}).get("cards", [])
        shop_id = tuple(c.get("label", "") for c in shop_cards)
        if shop_id == gs.last_logged_shop:
            return
        log_shop(state)
        ante_num, money = state.get("ante_num"), state.get("money", 0)

        def offer(c, item_type):
            gs.shop_events.append({
                "ante": ante_num, "event_type": "offer", "item_name": c.get("label", "?"),
                "item_type": item_type, "cost": c.get("cost", {}).get("buy"), "money_after": money,
            })

        for c in shop_cards:
            offer(c, c.get("set", "").lower())
        for c in state.get("packs", {}).get("cards", []):
            offer(c, "pack")
        for c in state.get("vouchers", {}).get("cards", []):
            offer(c, "voucher")
        gs.last_logged_shop = shop_id

    def _blind_transition(self, state: dict) -> None:
        """On a new blind to select: close the last one and start tracking the new one."""
        gs = self._gs
        blinds = state.get("blinds", {})
        blind_id = tuple(
            (k, b.get("name", ""), b.get("status", ""))
            for k, b in blinds.items() if isinstance(b, dict)
        )
        if blind_id == gs.last_logged_blind:
            return
        for b in blinds.values():
            if isinstance(b, dict) and b.get("status") in ("SELECT", "CURRENT"):
                name, score = b.get("name", "?"), b.get("score", "?")
                if gs.current_blind_name and gs.max_chips_this_blind > 0:
                    self._end_blind("WON")
                log_blind(name, score)
                gs.current_blind_name = name
                gs.current_blind_target = score
                gs.max_chips_this_blind = 0
                state.pop("_boss_disabled", None)
                gs.hands_at_blind_start = gs.total_hands_played
                gs.discards_at_blind_start = gs.total_discards_used
                # Balatro bumps ante_num once the boss is beaten, so the ante is taken
                # at blind start for the round's row.
                ante_now = state.get("ante_num")
                if ante_now is None:
                    log.warning(
                        "Blind %r started without ante_num in state — falling "
                        "back to last_ante=%s; round may be misattributed",
                        name, gs.last_ante,
                    )
                    gs.ante_at_blind_start = gs.last_ante or 0
                else:
                    gs.ante_at_blind_start = ante_now
                gs.hand_context_logged = False
                break
        gs.last_logged_blind = blind_id

    def _hand_state(self, state: dict) -> None:
        """The hand once per change, and the blind's context at its first hand."""
        gs = self._gs
        hand_cards = state.get("hand", {}).get("cards", [])
        hand_id = tuple(c.get("label", "") for c in hand_cards)
        if hand_id == gs.last_logged_hand:
            return
        log_hand(hand_cards, gs.prev_hand_labels)
        gs.prev_hand_labels = [c.get("label", "") for c in hand_cards]
        gs.last_logged_hand = hand_id
        if not gs.hand_context_logged and gs.current_blind_name:
            log_hand_context(state, gs.current_blind_name, gs.current_blind_target)
            gs.hand_context_logged = True

    def _joker_changes(self, state: dict) -> None:
        """A changed roster logs the new strategy; selling Luchador in a boss blind
        disables the boss for the rest of the round."""
        gs = self._gs
        cur_joker_keys = {joker_key(j) for j in state.get("jokers", {}).get("cards", [])}
        if cur_joker_keys != gs.prev_joker_keys and gs.prev_joker_keys:
            from balatro_bot.strategy import compute_strategy
            new_strat = compute_strategy(state.get("jokers", {}).get("cards", []), state.get("hands", {}))
            log.info("[STRAT] %s", new_strat.describes())
            if "j_luchador" in gs.prev_joker_keys and "j_luchador" not in cur_joker_keys:
                if gs.current_blind_name in BOSS_BLINDS:
                    state["_boss_disabled"] = True
                    log.info("[BOSS] Luchador sold — %s effect disabled", gs.current_blind_name)
        gs.prev_joker_keys = cur_joker_keys

    # -- each action --------------------------------------------------------

    def sending(
        self, game_state: str, method: str, params: dict | None, action: object,
        decision: dict | None, state: dict,
    ) -> None:
        """Log an action about to be sent and read what it acts on from ``state``."""
        gs = self._gs
        decision_state = (
            build_decision_state(gs, state)
            if decision is not None and not decision.get("continuation") else None
        )
        log_action(gs.actions_taken, game_state, method, params, action,
                   format_card_detail(method, params, state), state, gs.current_blind_target)

        p = params or {}
        chips = state.get("round", {}).get("chips", 0)
        pending = _Pending(
            game_state, method, params, action, decision, decision_state,
            money=state.get("money", 0), chips=chips,
            hand=state.get("hand", {}).get("cards", []) if method == "use" else None,
            play_snapshot=None,
        )
        if method == "sell" and "joker" in p:
            pending.selling_joker = _label_at(state.get("jokers", {}).get("cards", []) or [], p["joker"])
        if method == "buy" and "card" in p:
            shop_cards = state.get("shop", {}).get("cards", [])
            if p["card"] < len(shop_cards):
                card_set = shop_cards[p["card"]].get("set", "")
                label = shop_cards[p["card"]].get("label", "?")
                if card_set == "JOKER":
                    pending.buying_joker = label
                elif card_set in ("TAROT", "PLANET", "SPECTRAL"):
                    pending.buying_consumable = {"name": label, "type": card_set.lower()}
        if method == "buy" and "voucher" in p:
            label = _label_at(state.get("vouchers", {}).get("cards", []), p["voucher"])
            if label is not None:
                pending.buying_consumable = {"name": label, "type": "voucher"}
        if method == "buy" and "pack" in p:
            pending.buying_pack = _label_at(state.get("packs", {}).get("cards", []), p["pack"])
        if method == "pack" and "card" in p:
            pack_open = state.get("pack", {}).get("cards", [])
            if p["card"] < len(pack_open):
                pending.picking_label = pack_open[p["card"]].get("label", "?")
                pending.picking_set = pack_open[p["card"]].get("set")
        if method == "use" and "consumable" in p:
            pending.using_consumable = _label_at(state.get("consumables", {}).get("cards", []), p["consumable"])
        if method == "play":
            _scoring_log.info("  PRE_PLAY chips_in_round=%d", chips)
            pending.play_snapshot = build_play_snapshot(state, params, action)
        self._pending = pending

    def refused(self, error) -> None:
        """The game refused the action being sent."""
        a, self._pending = self._pending, None
        if a.decision is not None:
            record_decision(self._gs, a.decision, a.decision_state, a.game_state, a.method,
                            a.params, a.money, None, error=error.name)

    def timed_out(self) -> None:
        """The game stopped answering the action; a play is taken to have gone through."""
        a, self._pending = self._pending, None
        if a.method == "play":
            self._gs.total_hands_played += 1

    def acted(self, state: dict, recording_at: int | None = None) -> None:
        """The game took the action being sent and answered with ``state``."""
        gs = self._gs
        a, self._pending = self._pending, None
        method, params = a.method, a.params
        gs.actions_taken += 1

        detail = params
        if method == "use" and params and "cards" in params:
            hand_cards = a.hand or []
            labels = []
            for ci in params["cards"]:
                if ci < len(hand_cards):
                    v = hand_cards[ci].get("value", {})
                    labels.append(f"{v.get('rank', '?')} of {v.get('suit', '?')}")
            detail = dict(params, target_labels=labels)
        if method == "use" and params and "consumable" in params:
            detail = dict(detail or params, consumable_name=a.using_consumable)
        gs.action_log.append({
            "seq": gs.actions_taken,
            "game_state": a.game_state,
            "action_type": method,
            "detail": detail,
            "ante": (gs.ante_at_blind_start or state.get("ante_num"))
                    if method in ("play", "discard") else state.get("ante_num"),
            "blind_name": gs.current_blind_name,
            "chips_before": a.chips,
            "chips_after": state.get("round", {}).get("chips", 0),
            "money_before": a.money,
            "money_after": state.get("money", 0),
        })
        if a.decision is not None:
            record_decision(gs, a.decision, a.decision_state, a.game_state, method, params,
                            a.money, state.get("money", 0))
            if recording_at is not None and a.decision.get("policy", "greedy") != "greedy":
                gs.explored_actions.append(recording_at)

        if method == "play":
            self._record_play(a, state)
        elif method == "discard":
            gs.total_discards_used += 1

        ante = state.get("ante_num")
        if a.buying_joker:
            gs.jokers_bought.append({"joker_name": a.buying_joker, "buy_ante": state.get("ante_num", 0), "source": "shop"})
        if method == "pack" and a.picking_set == "JOKER" and a.picking_label:
            gs.jokers_bought.append({"joker_name": a.picking_label, "buy_ante": state.get("ante_num", 0), "source": "pack"})
        if a.buying_consumable:
            name, ctype = a.buying_consumable["name"], a.buying_consumable["type"]
            for c in gs.consumables_bought:
                if c["name"] == name and c["consumable_type"] == ctype:
                    c["count"] += 1
                    break
            else:
                gs.consumables_bought.append({"name": name, "consumable_type": ctype, "count": 1})

        money_after = state.get("money", 0)
        cost = a.money - money_after

        def event(event_type, item_name, item_type, cost=None, **extra):
            gs.shop_events.append({
                "ante": ante, "event_type": event_type, "item_name": item_name,
                "item_type": item_type, **extra, "cost": cost, "money_after": money_after,
            })

        if a.buying_joker:
            event("buy", a.buying_joker, "joker", cost)
        if a.buying_consumable:
            event("buy", a.buying_consumable["name"], a.buying_consumable["type"], cost)
        if method == "sell":
            self._record_sell(a, state, event)
        if method == "reroll":
            event("reroll", None, None, cost)
        if a.game_state == "BLIND_SELECT" and method in ("select", "skip"):
            name, target = gs.current_blind_name, gs.current_blind_target
            gs.blind_decisions.append({
                "ante": ante,
                "round": state.get("round_num"),
                "blind_name": name,
                "blind_target": target if isinstance(target, int) else None,
                "is_boss": bool(name and name not in ("Small Blind", "Big Blind")),
                "action": method,
                "money_at_decision": a.money,
                "joker_count": len(state.get("jokers", {}).get("cards", []) or []),
            })
        if a.buying_pack is not None:
            event("buy", a.buying_pack, "pack", cost)
        if method == "pack" and params:
            if params.get("skip"):
                event("skip", None, "pack", pack_type=gs.current_pack_label)
            elif "card" in params:
                event("pick", a.picking_label, "pack", pack_type=gs.current_pack_label)

        self._joker_changes(state)

    def _record_play(self, a: _Pending, state: dict) -> None:
        gs = self._gs
        gs.total_hands_played += 1
        hand_name = getattr(a.action, "hand_name", "") or ""
        if hand_name:
            gs.hand_types_played[hand_name] += 1
        post_chips = state.get("round", {}).get("chips", 0)
        # A hand that ends the round can be the last time its chips are visible.
        if post_chips > gs.max_chips_this_blind:
            gs.max_chips_this_blind = post_chips
        _scoring_log.info("  POST_PLAY chips_in_round=%d, delta=%d", post_chips, post_chips - a.chips)
        log_played_hand(a.play_snapshot, a.chips, state)
        gs.hands_scored += 1
        actual_score = post_chips - a.chips
        try:
            joker_contribs = serialize_joker_contributions(compute_played_hand_detail(a.play_snapshot))
        except Exception:
            joker_contribs = None
        gs.hand_scores.append({
            "seq": gs.hands_scored,
            "ante": gs.ante_at_blind_start or state.get("ante_num"),
            "blind_name": gs.current_blind_name,
            "hand_type": hand_name or None,
            "total_score": actual_score if actual_score > 0 else None,
            "joker_contributions": joker_contribs,
        })

    def _record_sell(self, a: _Pending, state: dict, event) -> None:
        sold_name = getattr(a.action, "reason", "") or "?"
        item_type: str | None = None
        if "joker" in (a.params or {}):
            item_type = "joker"
            if a.selling_joker:
                sold_name = a.selling_joker
                # The earliest unsold buy of that joker is the one sold.
                for jb in self._gs.jokers_bought:
                    if jb["joker_name"] == a.selling_joker and jb.get("sell_ante") is None:
                        jb["sell_ante"] = state.get("ante_num")
                        break
        elif "consumable" in (a.params or {}):
            item_type = "consumable"
            sold_name = "consumable"
        event("sell", sold_name, item_type)

    # -- the end ------------------------------------------------------------

    def finish(self, state: dict) -> None:
        """Record the end of the game from its final state."""
        gs = self._gs
        gs.max_chips_this_blind = max(gs.max_chips_this_blind, state.get("round", {}).get("chips", 0))
        self._end_blind("LOST")
        log_game_over(state, won=gs.actually_won, actions=gs.actions_taken,
                      hands=gs.total_hands_played, discards=gs.total_discards_used)
        if gs.actually_won:
            return
        last_lost = next((r for r in reversed(gs.round_results) if not r.get("won", False)), None)
        if last_lost:
            gs.death_info = {
                "ante": last_lost.get("ante"),
                "blind_name": last_lost.get("blind_name"),
                "is_boss": last_lost.get("is_boss"),
                "scored": last_lost.get("scored"),
                "needed": last_lost.get("needed"),
                "deficit": (last_lost.get("needed") or 0) - (last_lost.get("scored") or 0),
                "hands_left_at_death": last_lost.get("hands_used"),
                "discards_left_at_death": last_lost.get("discards_used"),
            }

    def payload(
        self, state: dict, *, replayable: bool, port: int | None, deck: str, stake: str,
        log_text: str | None,
    ) -> dict[str, Any]:
        """The dashboard's record of the finished game. A replayable game sends only its
        summary: the replay rebuilds the rest (ADR 0011)."""
        gs = self._gs
        joker_cards = state.get("jokers", {}).get("cards", [])
        final_roster = {j.get("label", "?") for j in joker_cards}

        def count(event_type, item_type=None):
            return sum(
                1 for e in gs.shop_events
                if e.get("event_type") == event_type and (item_type is None or e.get("item_type") == item_type)
            )

        def scaling(name, key):
            return (gs.joker_scaling.get(name) or {}).get(key)

        bought = {jb["joker_name"] for jb in gs.jokers_bought}
        return {
            "instance_port": port,
            "seed": state.get("seed", ""),
            "deck": deck,
            "stake": stake,
            "win": gs.actually_won,
            "final_ante": state.get("ante_num", 0),
            "final_round": state.get("round_num", 0),
            "actions": gs.actions_taken,
            "rerolls": count("reroll"),
            "packs_bought": count("buy", "pack"),
            "packs_opened": gs.packs_opened,
            "pack_picks": count("pick", "pack"),
            "pack_skips": count("skip", "pack"),
            "final_money": state.get("money", 0),
            "final_jokers": ", ".join(j.get("label", "?") for j in joker_cards),
            "total_hands": gs.total_hands_played,
            "total_discards": gs.total_discards_used,
            "log_text": None if replayable else log_text,
            "rounds": gs.round_results,
            "jokers": [
                {
                    "joker_name": jb["joker_name"],
                    "in_final_roster": jb["joker_name"] in final_roster,
                    "buy_ante": jb["buy_ante"],
                    "sell_ante": jb.get("sell_ante"),
                    "final_chips": scaling(jb["joker_name"], "chips"),
                    "final_mult": scaling(jb["joker_name"], "mult"),
                    "final_xmult": scaling(jb["joker_name"], "xmult"),
                    "source": jb.get("source"),
                }
                for jb in gs.jokers_bought + [
                    {"joker_name": name, "buy_ante": None, "source": "other"}
                    for name in sorted(final_roster - bought)
                ]
            ],
            "hand_types": dict(gs.hand_types_played),
            "consumables": gs.consumables_bought,
            "actions_log": [] if replayable else gs.action_log,
            "ante_snapshots": gs.ante_snapshots,
            "hand_scores": [] if replayable else gs.hand_scores,
            "shop_events": [] if replayable else gs.shop_events,
            "earnings": state.get("earnings", []),
            "shop_offers": [] if replayable else gs.shop_offers,
            "pack_offers": [] if replayable else gs.pack_offers,
            "tags_seen": gs.tags_seen,
            "blind_decisions": gs.blind_decisions,
            "death_info": gs.death_info,
            "final_deck": deck_composition(state.get("cards", {}).get("cards", []) or []),
            "decisions": [] if replayable else gs.decisions,
        }
