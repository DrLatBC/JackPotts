from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Scoring-log-specific patterns (local to this module)
_RE_SCORE_HAND = re.compile(
    r"(?:^|\s)(High Card|Pair|Two Pair|Three of a Kind|Straight|Flush|Full House"
    r"|Four of a Kind|Straight Flush|Five of a Kind|Flush House|Flush Five)\s+\["
)
_RE_SCORE_ACTUAL = re.compile(r"actual=(\d+)")
_RE_SCORE_JOKERS = re.compile(r"jokers: \[([^\]]*)\]")
_RE_SCORE_MISMATCH = re.compile(r"MISMATCH\(diff=([+-]?\d+)\)")
_RE_SCORE_SCORING = re.compile(r"scoring=\[([^\]]+)\]\(\d+\)")
_RE_SCORE_ENHS = re.compile(r"enhs=\[([^\]]*)\]")
_RE_SCORE_SEALS = re.compile(r"seals=\[([^\]]*)\]")
_RE_SCORE_EDS = re.compile(r"eds=\[([^\]]*)\]")
_RE_SCORE_BLIND = re.compile(r"blind=(.+?)\s+ante=")
_RE_SCORE_ANTE = re.compile(r"ante=(\d+)")
_RE_SCORE_HANDS_LEFT = re.compile(r"hands_left=(\d+)")
# Rank: digits or face letter immediately before a suit symbol
_RE_RANK = re.compile(r"(10|[2-9JQKA])[\u2660\u2665\u2666\u2663]")
_RE_SUIT = re.compile(r"[\u2660\u2665\u2666\u2663]")
_SUIT_NAMES = {"\u2660": "Spades", "\u2665": "Hearts", "\u2666": "Diamonds", "\u2663": "Clubs"}


def _extract_joker_labels(joker_str: str) -> list[str]:
    """Extract joker labels from a contributions string like 'Campfire(x2.50), Madness(+4.0m)'."""
    if not joker_str or joker_str.strip() == "none":
        return []
    return [part.split("(")[0].strip() for part in joker_str.split(",") if "(" in part]


def _extract_csv(field: str) -> list[str]:
    """Split a comma-separated attribute field, filtering empty strings."""
    return [v.strip() for v in field.split(",") if v.strip()]


def parse_scoring_log(path: Path) -> dict:
    """Parse a scoring log for estimate vs actual accuracy."""
    empty: dict = {
        "total_scores": 0, "mismatches": 0, "mismatch_diffs": [],
        "scored_by_hand": Counter(), "mismatch_by_hand": Counter(),
        "joker_in_all": Counter(), "joker_in_mismatch": Counter(),
        "rank_in_all": Counter(), "rank_in_mismatch": Counter(),
        "suit_in_all": Counter(), "suit_in_mismatch": Counter(),
        "enh_in_all": Counter(), "enh_in_mismatch": Counter(),
        "seal_in_all": Counter(), "seal_in_mismatch": Counter(),
        "ed_in_all": Counter(), "ed_in_mismatch": Counter(),
        "blind_in_all": Counter(), "blind_in_mismatch": Counter(),
        "ante_in_all": Counter(), "ante_in_mismatch": Counter(),
        "hands_left_in_all": Counter(), "hands_left_in_mismatch": Counter(),
        "actual_values": [],
    }
    if not path.exists():
        return empty

    text = path.read_text(encoding="utf-8", errors="replace")
    total = 0
    mismatches = 0
    diffs = []
    scored_by_hand: Counter = Counter()
    mismatch_by_hand: Counter = Counter()
    joker_in_all: Counter = Counter()
    joker_in_mismatch: Counter = Counter()
    rank_in_all: Counter = Counter()
    rank_in_mismatch: Counter = Counter()
    suit_in_all: Counter = Counter()
    suit_in_mismatch: Counter = Counter()
    enh_in_all: Counter = Counter()
    enh_in_mismatch: Counter = Counter()
    seal_in_all: Counter = Counter()
    seal_in_mismatch: Counter = Counter()
    ed_in_all: Counter = Counter()
    ed_in_mismatch: Counter = Counter()
    blind_in_all: Counter = Counter()
    blind_in_mismatch: Counter = Counter()
    ante_in_all: Counter = Counter()
    ante_in_mismatch: Counter = Counter()
    hands_left_in_all: Counter = Counter()
    hands_left_in_mismatch: Counter = Counter()
    actual_values: list[int] = []

    for line in text.splitlines():
        if "est=" not in line or "actual=" not in line:
            continue
        if "MISMATCH_NOISE" in line:
            continue
        total += 1

        hm = _RE_SCORE_HAND.search(line)
        hand_type = hm.group(1) if hm else "Unknown"
        scored_by_hand[hand_type] += 1

        am = _RE_SCORE_ACTUAL.search(line)
        if am:
            actual_values.append(int(am.group(1)))

        jm = _RE_SCORE_JOKERS.search(line)
        if jm:
            for label in _extract_joker_labels(jm.group(1)):
                joker_in_all[label] += 1

        # Rank and suit from scoring=[...] field (set-per-hand to avoid hand-size bias)
        sm = _RE_SCORE_SCORING.search(line)
        hand_ranks: set[str] = set()
        hand_suits: set[str] = set()
        if sm:
            scoring_field = sm.group(1)
            hand_ranks = set(_RE_RANK.findall(scoring_field))
            hand_suits = {_SUIT_NAMES.get(s, s) for s in _RE_SUIT.findall(scoring_field)}
        for r in hand_ranks:
            rank_in_all[r] += 1
        for s in hand_suits:
            suit_in_all[s] += 1

        # Enhancement, seal, edition from new log fields (set-per-hand)
        enh_m = _RE_SCORE_ENHS.search(line)
        hand_enhs: list[str] = _extract_csv(enh_m.group(1)) if enh_m else []
        seal_m = _RE_SCORE_SEALS.search(line)
        hand_seals: list[str] = _extract_csv(seal_m.group(1)) if seal_m else []
        ed_m = _RE_SCORE_EDS.search(line)
        hand_eds: list[str] = _extract_csv(ed_m.group(1)) if ed_m else []
        for v in hand_enhs:
            enh_in_all[v] += 1
        for v in hand_seals:
            seal_in_all[v] += 1
        for v in hand_eds:
            ed_in_all[v] += 1

        # Blind, ante, hands_left (may be absent in older logs)
        blind_m = _RE_SCORE_BLIND.search(line)
        blind_val = blind_m.group(1) if blind_m else None
        ante_m = _RE_SCORE_ANTE.search(line)
        ante_val = ante_m.group(1) if ante_m else None
        hl_m = _RE_SCORE_HANDS_LEFT.search(line)
        hl_val = hl_m.group(1) if hl_m else None
        if blind_val:
            blind_in_all[blind_val] += 1
        if ante_val:
            ante_in_all[ante_val] += 1
        if hl_val:
            hands_left_in_all[hl_val] += 1

        mm = _RE_SCORE_MISMATCH.search(line)
        if mm:
            mismatches += 1
            diff = int(mm.group(1))
            diffs.append(diff)
            mismatch_by_hand[hand_type] += 1
            if jm:
                for label in _extract_joker_labels(jm.group(1)):
                    joker_in_mismatch[label] += 1
            for r in hand_ranks:
                rank_in_mismatch[r] += 1
            for s in hand_suits:
                suit_in_mismatch[s] += 1
            for v in hand_enhs:
                enh_in_mismatch[v] += 1
            for v in hand_seals:
                seal_in_mismatch[v] += 1
            for v in hand_eds:
                ed_in_mismatch[v] += 1
            if blind_val:
                blind_in_mismatch[blind_val] += 1
            if ante_val:
                ante_in_mismatch[ante_val] += 1
            if hl_val:
                hands_left_in_mismatch[hl_val] += 1

    return {
        "total_scores": total, "mismatches": mismatches, "mismatch_diffs": diffs,
        "scored_by_hand": scored_by_hand, "mismatch_by_hand": mismatch_by_hand,
        "joker_in_all": joker_in_all, "joker_in_mismatch": joker_in_mismatch,
        "rank_in_all": rank_in_all, "rank_in_mismatch": rank_in_mismatch,
        "suit_in_all": suit_in_all, "suit_in_mismatch": suit_in_mismatch,
        "enh_in_all": enh_in_all, "enh_in_mismatch": enh_in_mismatch,
        "seal_in_all": seal_in_all, "seal_in_mismatch": seal_in_mismatch,
        "ed_in_all": ed_in_all, "ed_in_mismatch": ed_in_mismatch,
        "blind_in_all": blind_in_all, "blind_in_mismatch": blind_in_mismatch,
        "ante_in_all": ante_in_all, "ante_in_mismatch": ante_in_mismatch,
        "hands_left_in_all": hands_left_in_all, "hands_left_in_mismatch": hands_left_in_mismatch,
        "actual_values": actual_values,
    }
