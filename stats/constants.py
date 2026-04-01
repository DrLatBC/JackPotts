from __future__ import annotations

import re

UTILITY_JOKERS = {
    "Juggler", "Drunkard", "Four Fingers", "Smeared Joker", "Shortcut",
    "Splash", "Chicot", "Mr. Bones", "Perkeo", "Cartomancer",
    "Hallucination", "Space Joker", "8 Ball", "Oops! All 6s",
    "Pareidolia", "Hack", "Seltzer", "Invisible Joker", "Luchador",
    "Diet Cola", "Turtle Bean", "Burnt Joker", "Merry Andy",
}
XMULT_JOKERS = {
    "Cavendish", "Joker Stencil", "The Duo", "The Trio", "The Family",
    "The Order", "The Tribe", "Acrobat", "Blackboard", "Flower Pot",
    "Madness", "Constellation", "Campfire", "Vampire", "Hologram",
}

PLANET_NAMES = {
    "Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn",
    "Uranus", "Neptune", "Pluto", "Planet X", "Ceres", "Eris",
}

KNOWN_HAND_TYPES = {
    "High Card", "Pair", "Two Pair", "Three of a Kind", "Straight",
    "Flush", "Full House", "Four of a Kind", "Straight Flush",
    "Five of a Kind", "Flush House", "Flush Five",
}

# Regex patterns compiled once
RE_GAME_OVER = re.compile(r"Game over: (\w+).*ante=(\d+).*round=(\d+).*?(\d+) actions")
RE_SUMMARY = re.compile(r"Summary: \$(\d+) \| jokers: \[([^\]]*)\] \| hands=(\d+) discards=(\d+)")
RE_ROUND = re.compile(
    r"\[ROUND\] (.+?): scored ([\d,]+) / needed ([\d,]+)"
    r" .+ (WON|LOST) \| (\d+) hands?, (\d+) discards?"
)
RE_PLAY = re.compile(r"SELECTING_HAND -> play\(.+?\| (.+?) for (\d+)")
RE_BEST_AVAILABLE = re.compile(r"best available: (.+?) for (\d+)")
RE_MILK_PLAY = re.compile(r"milk: (.+?) for (\d+)")
RE_MOUTH_LOCKED = re.compile(r"playing (.+?) for (\d+)")
RE_JOKER_BUY = re.compile(r"buy joker: (.+?) for \$")
RE_JOKER_SELL = re.compile(r"sell\(\{'joker'.*\| sell (.+?) \(value=")
RE_JOKER_SELL_FOR = re.compile(r"\) for (.+?) \(value=")
RE_CONSUMABLE_BUY = re.compile(r"buy consumable: (.+?)(?:\s+for\s+\$\d+)?\s+\(")
RE_VOUCHER_BUY = re.compile(r"buy voucher: (.+?) for \$")
RE_BLIND = re.compile(r"Blind: (.+?) \(need")
RE_MILK_ACTION = re.compile(r"milk: (.+?) \(")
RE_REROLL = re.compile(r"SHOP -> reroll\(\)")
RE_GAME_START = re.compile(r"Started new game: deck=(\w+) stake=(\w+)")
RE_ROSTER = re.compile(r"\[ANTE \d+\] Roster \(\d+ jokers\): \[(.+)\]")
RE_BEST_AVAILABLE_PLAY = re.compile(r"SELECTING_HAND -> play.+\| best available:")
RE_DESPERATION_CYCLE = re.compile(r"desperation cycle:")
RE_TAROT_USE = re.compile(r"\| use tarot: (.+?) ->|\| use tarot: (.+?) \(")
RE_TAROT_DESPERATE = re.compile(r"\| desperate: (.+?) \(")
RE_ROSTER_SCALING = re.compile(r"(\w[\w\s]+?)\(([+X][\d.]+(?:chips|mult)?)\)")
RE_SHOP_MONEY = re.compile(r"Shop \(\$(\d+)\):")
RE_PACK_PICK = re.compile(r"SMODS_BOOSTER_OPENED -> pack\(\{'card'")
RE_PACK_SKIP = re.compile(r"SMODS_BOOSTER_OPENED -> pack\(\{'skip': True")
