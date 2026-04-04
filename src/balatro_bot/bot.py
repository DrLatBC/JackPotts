"""Main bot loop — connects to balatrobot and runs the decision engine."""

from __future__ import annotations

import logging
import logging.handlers
import random
import string
import time
from collections import defaultdict
import httpx
from balatrobot import APIError, BalatroClient

from balatro_bot.engine import RuleEngine

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
        from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
        from balatro_bot.domain.scoring.estimate import score_hand_detailed

        played = snapshot["played"]
        hand_name = snapshot["hand_name"]
        if not hand_name:
            hand_name = classify_hand(played)

        joker_keys = {j.get("key") for j in snapshot["jokers"]}
        has_splash = "j_splash" in joker_keys
        four_fingers = "j_four_fingers" in joker_keys
        smeared = "j_smeared" in joker_keys
        shortcut = "j_shortcut" in joker_keys
        scoring = played if has_splash else _scoring_cards_for(hand_name, played, four_fingers=four_fingers, smeared=smeared, shortcut=shortcut)

        # Apply boss blind level adjustments for scoring estimate
        # NOTE: The Flint halving is already applied in the snapshot (line ~675),
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
                chips = card_chip_value(c)
                mult = card_mult_value(c)
                xmult = card_xmult_value(c)
                perma = c.get("value", {}).get("perma_bonus", 0) or 0
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
                    i, j.get("key", "?"), rarity, jed_str, ability, parsed,
                    effect[:120] if effect else "-",
                )
    except Exception as e:
        _scoring_log.warning("scoring log error: %s", e)


def _format_deck_snapshot(deck_cards: list) -> str:
    """Format a compact deck snapshot for logging at ante transitions."""
    ENHANCEMENT_ABBR = {
        "GLASS": "GL", "STEEL": "ST", "WILD": "WL",
        "BONUS": "BN", "GOLD": "GD", "LUCKY": "LK", "MULT": "MU",
    }
    SEAL_ABBR = {
        "Red Seal": "RS", "Gold Seal": "GS",
        "Blue Seal": "BS", "Purple Seal": "PS",
    }
    SUIT_SYM = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
    RANK_ORDER = ["A", "K", "Q", "J", "T", "9", "8", "7", "6", "5", "4", "3", "2"]

    by_rank: dict[str, list] = defaultdict(list)
    stone_count = 0

    for card in deck_cards:
        mod = card.get("modifier", {})
        if not isinstance(mod, dict):
            mod = {}
        enh = mod.get("enhancement", "")
        if enh == "STONE":
            stone_count += 1
            continue
        rank = card.get("value", {}).get("rank")
        suit = card.get("value", {}).get("suit")
        if not rank or not suit:
            continue
        seal = mod.get("seal", "")
        by_rank[rank].append((suit, enh, seal))

    parts = []
    for rank in RANK_ORDER:
        if rank not in by_rank:
            continue
        inner = rank
        for suit, enh, seal in sorted(by_rank[rank], key=lambda x: x[0]):
            sym = SUIT_SYM.get(suit, suit)
            suffix = ENHANCEMENT_ABBR.get(enh, "") + SEAL_ABBR.get(seal, "")
            inner += sym + suffix
        parts.append(f"[{inner}]")

    if stone_count:
        parts.append(f"[STN×{stone_count}]")

    total = sum(len(v) for v in by_rank.values()) + stone_count
    return f"({total}): " + " ".join(parts)


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
                "VICTORY at ante %s round %s (seed=%s) — entering endless mode",
                cur_ante, state.get("round_num", "?"),
                state.get("seed", "?"),
            )
            if current_blind_name:
                target = current_blind_target if isinstance(current_blind_target, int) else 0
                log.info(
                    "[ROUND] %s: scored %s / needed %s — WON | %d hands, %d discards",
                    current_blind_name, f"{max_chips_this_blind:,}",
                    f"{target:,}",
                    total_hands_played - hands_at_blind_start,
                    total_discards_used - discards_at_blind_start,
                )
                current_blind_name = None

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

            deck_cards = state.get("cards", {}).get("cards", [])
            log.info("[ANTE %s] Deck %s", ante_num, _format_deck_snapshot(deck_cards))

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
        # Track highest chips seen during this blind (capture before round resets)
        cur_chips = state.get("round", {}).get("chips", 0)
        if cur_chips > max_chips_this_blind:
            max_chips_this_blind = cur_chips
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
                        # Log previous blind summary before overwriting
                        if current_blind_name and max_chips_this_blind > 0:
                            log.info(
                                "[ROUND] %s: scored %s / needed %s — WON | %d hands, %d discards",
                                current_blind_name, f"{max_chips_this_blind:,}",
                                f"{current_blind_target:,}" if isinstance(current_blind_target, int) else current_blind_target,
                                total_hands_played - hands_at_blind_start,
                                total_discards_used - discards_at_blind_start,
                            )
                        log.info("Blind: %s (need %s chips)", name, score)
                        current_blind_name = name
                        current_blind_target = score
                        max_chips_this_blind = 0
                        state.pop("_boss_disabled", None)
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
                    from balatro_bot.domain.scoring.search import best_hand as _bh
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
            _scoring_log.info("  PRE_PLAY chips_in_round=%d", pre_play_chips)
            hand_cards_snap = state.get("hand", {}).get("cards", [])
            play_indices = set(params.get("cards", []))
            # Detect The Flint and halve hand levels for accurate scoring
            snap_hand_levels = state.get("hands", {})
            for b in state.get("blinds", {}).values():
                if isinstance(b, dict) and b.get("status") == "CURRENT" and b.get("name") == "The Flint":
                    from balatro_bot.domain.scoring.base import flint_halve_hand_levels
                    snap_hand_levels = flint_halve_hand_levels(snap_hand_levels)
                    break

            # Detect current blind name for snapshot
            snap_blind_name = ""
            for b in state.get("blinds", {}).values():
                if isinstance(b, dict) and b.get("status") == "CURRENT":
                    snap_blind_name = b.get("name", "")
                    break

            play_snapshot = {
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

        # Capture expected hand score for chip accounting on timeout
        expected_hand_score = 0
        if method == "play":
            import re
            m = re.search(r"for (\d+)", getattr(action, "reason", ""))
            if m:
                expected_hand_score = int(m.group(1))

        # Shorter timeout on ante 8 plays — the win screen hangs the mod
        saved_timeout = None
        if method == "play" and cur_ante == 8:
            saved_timeout = client.timeout
            client.timeout = 5.0

        try:
            state = client.call(method, params)
            actions_taken += 1
            if method == "play":
                total_hands_played += 1
                post_chips = state.get("round", {}).get("chips", 0)
                _scoring_log.info("  POST_PLAY chips_in_round=%d, delta=%d", post_chips, post_chips - pre_play_chips)
                # The Hook discards 2 held cards BEFORE scoring (before
                # cards are replenished).  The pre-play snapshot is used
                # as-is; joker corrections (Ramen, Yorick, Green Joker)
                # are applied in joker_effects/complex.py.
                _log_played_hand(play_snapshot, pre_play_chips, state, fmt_card)
            elif method == "discard":
                total_discards_used += 1

            # Detect joker roster changes and log strategy shift
            cur_joker_keys = {j.get("key") for j in state.get("jokers", {}).get("cards", [])}
            if cur_joker_keys != prev_joker_keys and prev_joker_keys:
                from balatro_bot.strategy import compute_strategy as _cs
                new_strat = _cs(state.get("jokers", {}).get("cards", []), state.get("hands", {}))
                log.info("[STRAT] %s", new_strat.describes())
                # Luchador sold during a boss blind → boss effect disabled
                if "j_luchador" in prev_joker_keys and "j_luchador" not in cur_joker_keys:
                    from balatro_bot.domain.policy.playing import BOSS_BLINDS
                    if current_blind_name in BOSS_BLINDS:
                        state["_boss_disabled"] = True
                        log.info("[BOSS] Luchador sold — %s effect disabled", current_blind_name)
            prev_joker_keys = cur_joker_keys

            consecutive_errors = 0
        except httpx.TimeoutException:
            if method == "play":
                total_hands_played += 1  # hand was played, game processed it
            log.warning("Timeout on %s(%s) — re-polling gamestate", method, params)
            try:
                state = client.call("gamestate")
                # Check if game ended during the timeout
                if state.get("state") == "GAME_OVER":
                    break
                # Victory detection: if ante advanced to 9+ during a play
                # timeout, the win screen caused the hang — handle it here
                post_ante = state.get("ante_num", 0)
                if post_ante >= 9 and not actually_won:
                    actually_won = True
                    if current_blind_name:
                        target = current_blind_target if isinstance(current_blind_target, int) else 0
                        final_chips = pre_play_chips + expected_hand_score
                        max_chips_this_blind = max(max_chips_this_blind, final_chips)
                        log.info(
                            "VICTORY at ante %s round %s (seed=%s) — scored %s / needed %s — WON | %d hands, %d discards",
                            post_ante, state.get("round_num", "?"),
                            state.get("seed", "?"),
                            f"{final_chips:,}", f"{target:,}",
                            total_hands_played - hands_at_blind_start,
                            total_discards_used - discards_at_blind_start,
                        )
                        current_blind_name = None
                    else:
                        log.info(
                            "VICTORY at ante %s round %s (seed=%s)",
                            post_ante, state.get("round_num", "?"),
                            state.get("seed", "?"),
                        )
                # Diagnostic dump on timeout
                gs = state.get("state", "?")
                pack_cards = state.get("pack", {}).get("cards", [])
                pack_labels = [c.get("label", "?") for c in pack_cards]
                log.warning(
                    "  post-timeout state=%s pack=%s money=%s ante=%s round=%s",
                    gs, pack_labels or "none", state.get("money"),
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
            consecutive_errors += 1
            log.warning("API error: %s (%s) — retry %d", e.message, e.name, consecutive_errors)

            # If a consumable use was rejected, clear Fool tracking so
            # the rule engine doesn't keep trying the same consumable.
            if method == "use" and e.name == "NOT_ALLOWED":
                for rule in engine.rules.get(game_state, []):
                    if hasattr(rule, "_last_used_consumable"):
                        rule._last_used_consumable = None
                        break

            if consecutive_errors >= 5:
                log.error("Too many consecutive errors, forcing skip")
                if game_state in ("TAROT_PACK", "PLANET_PACK", "SPECTRAL_PACK",
                                  "STANDARD_PACK", "BUFFOON_PACK", "SMODS_BOOSTER_OPENED"):
                    try:
                        state = client.call("pack", {"skip": True})
                    except APIError:
                        pass
                state = client.call("gamestate")
                consecutive_errors = 0
            else:
                time.sleep(poll_interval)
                state = client.call("gamestate")
        finally:
            if saved_timeout is not None:
                client.timeout = saved_timeout

    # Capture chips from the final state — the loop exits before the top-of-loop
    # chips tracking can run, so the last play/discard response's chips are missed.
    final_chips = state.get("round", {}).get("chips", 0)
    if final_chips > max_chips_this_blind:
        max_chips_this_blind = final_chips

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
