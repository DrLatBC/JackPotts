"""Shared game lifecycle helpers for integration tests.

All functions take a BalatroClient as their first argument.
No classes — just stateless utility functions.

Usage from test scripts:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
    from harness import wait_for_state, setup_game, force_boss, ...

Server management:
    server = start_server(port=12846)   # launches balatrobot serve
    client = BalatroClient(port=12846)
    ...
    stop_server(server)                 # kills process tree
"""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time

# Ensure src/ is importable — fallback for when the caller's __main__ hasn't
# done its own sys.path hack yet.
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# balatrobot is a pip-installed package (the game server/client).  If we're
# running from the system Python rather than the activated venv, add the
# venv's site-packages so the import succeeds.
_venv_sp = os.path.join(os.path.dirname(__file__), "..", "..", ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_sp) and _venv_sp not in sys.path:
    sys.path.insert(0, os.path.abspath(_venv_sp))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.config import SupervisorConfig

_cfg = SupervisorConfig()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 500 away from supervisor's base_port to avoid collisions
TEST_PORT = _cfg.base_port + 500

# (display_name, internal_key, min_ante) — from game.lua P_BLINDS
_BOSS_DATA: list[tuple[str, str, int]] = [
    # Ante 1
    ("The Club",       "bl_club",           1),
    ("The Goad",       "bl_goad",           1),
    ("The Head",       "bl_head",           1),
    ("The Hook",       "bl_hook",           1),
    ("The Manacle",    "bl_manacle",        1),
    ("The Pillar",     "bl_pillar",         1),
    ("The Psychic",    "bl_psychic",        1),
    ("The Window",     "bl_window",         1),
    # Ante 2
    ("The Arm",        "bl_arm",            2),
    ("The Fish",       "bl_fish",           2),
    ("The Flint",      "bl_flint",          2),
    ("The House",      "bl_house",          2),
    ("The Mark",       "bl_mark",           2),
    ("The Mouth",      "bl_mouth",          2),
    ("The Needle",     "bl_needle",         2),
    ("The Wall",       "bl_wall",           2),
    ("The Water",      "bl_water",          2),
    ("The Wheel",      "bl_wheel",          2),
    # Ante 3
    ("The Eye",        "bl_eye",            3),
    ("The Tooth",      "bl_tooth",          3),
    # Ante 4
    ("The Plant",      "bl_plant",          4),
    # Ante 5
    ("The Serpent",    "bl_serpent",         5),
    # Ante 6
    ("The Ox",         "bl_ox",             6),
    # Ante 8 (showdown)
    ("Amber Acorn",    "bl_final_acorn",    8),
    ("Cerulean Bell",  "bl_final_bell",     8),
    ("Crimson Heart",  "bl_final_heart",    8),
    ("Verdant Leaf",   "bl_final_leaf",     8),
    ("Violet Vessel",  "bl_final_vessel",   8),
]

BOSS_KEYS: dict[str, str] = {name: key for name, key, _ in _BOSS_DATA}
BOSS_MIN_ANTE: dict[str, int] = {name: ante for name, _, ante in _BOSS_DATA}

# Track running servers for atexit cleanup
_running_servers: list[subprocess.Popen] = []


# ---------------------------------------------------------------------------
# Server lifecycle (mirrors supervisor.py pattern)
# ---------------------------------------------------------------------------

def _check_port(port: int, timeout: float = 2.0) -> bool:
    """TCP health check — True if something is listening on localhost:port."""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def start_server(port: int = TEST_PORT, *,
                 startup_timeout: float = 30.0,
                 headless: bool = True,
                 fast: bool = True) -> subprocess.Popen:
    """Launch a balatrobot server and wait until it's healthy.

    Uses the same paths as supervisor.py (from SupervisorConfig).
    Registers an atexit handler so the server is killed even on unclean exit.

    Returns the Popen object (pass to stop_server when done).
    """
    if _check_port(port):
        raise RuntimeError(
            f"Port {port} already in use — is another server running? "
            f"(supervisor uses {_cfg.base_port}+, tests use {TEST_PORT}+)"
        )

    cmd = [
        _cfg.uvx_path, "balatrobot", "serve",
        "--port", str(port),
        "--love-path", _cfg.love_path,
        "--lovely-path", _cfg.lovely_path,
    ]
    if headless:
        cmd.append("--headless")
    if fast:
        cmd.append("--fast")

    print(f"  Starting server on port {port}...")
    proc = subprocess.Popen(
        cmd,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    _running_servers.append(proc)

    # Wait for health
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Server exited immediately with code {proc.returncode}"
            )
        if _check_port(port):
            print(f"  Server healthy on port {port} (pid={proc.pid})")
            return proc
        time.sleep(1.0)

    # Timed out — kill and raise
    _kill_proc(proc)
    raise TimeoutError(
        f"Server on port {port} not healthy after {startup_timeout}s"
    )


def stop_server(proc: subprocess.Popen) -> None:
    """Kill a server process tree."""
    _kill_proc(proc)
    if proc in _running_servers:
        _running_servers.remove(proc)


def _kill_proc(proc: subprocess.Popen) -> None:
    """Kill a process tree (Windows: taskkill /F /T, fallback: proc.kill)."""
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    except OSError:
        pass
    time.sleep(1.0)
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def ensure_server(port: int = TEST_PORT, **kwargs) -> tuple[BalatroClient, subprocess.Popen | None]:
    """Return a (client, server_proc) pair.

    If a server is already listening on *port*, connects to it and returns
    server_proc=None (caller didn't start it, shouldn't kill it).
    Otherwise starts a new server and returns both.
    """
    if _check_port(port):
        print(f"  Server already running on port {port}")
        return BalatroClient(port=port), None

    proc = start_server(port, **kwargs)
    return BalatroClient(port=port), proc


def take_screenshot(client: BalatroClient, label: str,
                    out_dir: str = "test_screenshots") -> str | None:
    """Ask balatrobot to take a screenshot, saved with the given label.

    Returns the path if successful, None on failure.
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_label = label.replace(" ", "_").replace("/", "-")
    filename = f"{ts}_{safe_label}.png"
    filepath = os.path.join(out_dir, filename)
    try:
        result = client.call("screenshot", {"path": filepath})
        print(f"    SCREENSHOT: {filepath}")
        return filepath
    except APIError:
        # Some versions may not take a path param — try without
        try:
            result = client.call("screenshot")
            print(f"    SCREENSHOT: (server default location)")
            return str(result) if result else None
        except APIError as e:
            print(f"    Screenshot failed: {e.message}")
            return None


@atexit.register
def _cleanup_servers():
    """Kill any servers we started, even on crash / KeyboardInterrupt."""
    for proc in list(_running_servers):
        _kill_proc(proc)
    _running_servers.clear()


# ---------------------------------------------------------------------------
# State polling
# ---------------------------------------------------------------------------

def wait_for_state(client: BalatroClient, targets, max_tries: int = 40,
                   screenshot_label: str = "") -> dict:
    """Poll until the game reaches one of *targets* (set or list of state strings).

    Automatically advances through intermediate states (BLIND_SELECT, SHOP,
    ROUND_EVAL, transition states) so callers don't need to handle them.

    If screenshot_label is set and the wait times out, takes a screenshot before
    raising TimeoutError.
    """
    state: dict = {}
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in targets:
            return state
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
            continue
        if gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
            continue
        if gs == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
            time.sleep(0.3)
            continue
        # Transition states: HAND_PLAYED, DRAW_TO_HAND, NEW_ROUND
        time.sleep(0.3)
    if screenshot_label:
        take_screenshot(client, f"timeout_{screenshot_label}")
    raise TimeoutError(f"Never reached {targets}, stuck in {state.get('state')}")


# ---------------------------------------------------------------------------
# Game setup
# ---------------------------------------------------------------------------

def setup_game(client: BalatroClient, seed: str, *,
               deck: str = "RED", stake: str = "WHITE") -> dict:
    """Start a fresh game and wait until SELECTING_HAND.

    Minimal version — does NOT sell jokers, discard hand, or inject anything.
    Use setup_game_full() when you need a clean slate with injected cards.
    """
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": deck, "stake": stake, "seed": seed})
    return wait_for_state(client, {"SELECTING_HAND"})


def setup_game_full(client: BalatroClient, seed: str, *,
                    joker_keys: list | None = None,
                    card_configs: list[dict] | None = None,
                    hands_left: int | None = None,
                    deck: str = "RED", stake: str = "WHITE") -> dict:
    """Start a fresh game, sell default jokers, discard hand, inject cards/jokers.

    joker_keys: list of joker key strings or add-params dicts.
    card_configs: list of dicts with "key" + optional "edition"/"enhancement"/"seal".
    hands_left: if set, override remaining hands via set API.
    """
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": deck, "stake": stake, "seed": seed})
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Sell default jokers
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard hand twice to clear starting cards
    for _ in range(2):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass

    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

    if card_configs:
        for cfg in card_configs:
            params = {"key": cfg["key"]}
            for k in ("edition", "enhancement", "seal"):
                if k in cfg:
                    params[k] = cfg[k]
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED card {cfg['key']}: {e.message}")

    if hands_left is not None:
        try:
            client.call("set", {"hands": hands_left})
        except APIError as e:
            print(f"  FAILED to set hands={hands_left}: {e.message}")

    time.sleep(0.3)
    return client.call("gamestate")


# ---------------------------------------------------------------------------
# Blind inspection
# ---------------------------------------------------------------------------

def get_current_blind(state: dict) -> tuple[str, str]:
    """Return (name, key) of the CURRENT blind."""
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            return b.get("name", ""), b.get("key", "")
    return "", ""


def get_boss_name(state: dict) -> str:
    """Extract the boss blind display name from gamestate."""
    for key, b in state.get("blinds", {}).items():
        if isinstance(b, dict) and "boss" in key.lower():
            return b.get("name", "")
    return ""


def is_boss_blind_select(state: dict) -> bool:
    """True if we're at BLIND_SELECT with small+big already defeated."""
    blinds = state.get("blinds", {})
    small = blinds.get("small", {})
    big = blinds.get("big", {})
    return (isinstance(small, dict) and small.get("status") == "DEFEATED" and
            isinstance(big, dict) and big.get("status") == "DEFEATED")


# ---------------------------------------------------------------------------
# Fast-forward / cheat helpers
# ---------------------------------------------------------------------------

def beat_blind_fast(client: BalatroClient, state: dict) -> None:
    """Set chips to 999999 and play first 5 cards to instantly beat current blind."""
    try:
        client.call("set", {"chips": 999999})
    except APIError:
        pass
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        client.call("play", {"cards": list(range(min(5, len(hc))))})
    time.sleep(0.5)


def cheat_win_if_needed(client: BalatroClient, blind_name: str) -> None:
    """If still in SELECTING_HAND for the given blind, cheat to win it."""
    state = client.call("gamestate")
    gs = state.get("state", "")
    cur_blind, _ = get_current_blind(state)
    if gs == "SELECTING_HAND" and cur_blind == blind_name:
        print("    Cheating to win...")
        beat_blind_fast(client, state)
        time.sleep(0.5)


def advance_through_post_blind(client: BalatroClient) -> dict:
    """Advance through ROUND_EVAL → SHOP → next BLIND_SELECT/SELECTING_HAND."""
    for _ in range(30):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in ("SELECTING_HAND", "BLIND_SELECT"):
            return state
        if gs == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        elif gs == "GAME_OVER":
            return state
        else:
            time.sleep(0.3)
    return client.call("gamestate")


def advance_to_boss_select(client: BalatroClient, target_ante: int) -> dict:
    """Set ante, beat small+big blinds instantly, arrive at boss BLIND_SELECT."""
    try:
        client.call("set", {"ante": target_ante})
        print(f"  Set ante to {target_ante}")
    except APIError as e:
        print(f"  WARNING: set ante failed: {e.message}")

    try:
        client.call("set", {"money": 999999})
    except APIError:
        pass

    for _ in range(50):
        state = client.call("gamestate")
        gs = state.get("state", "")

        if gs == "SELECTING_HAND":
            blind_name, _ = get_current_blind(state)
            if blind_name not in ("Small Blind", "Big Blind"):
                return state
            beat_blind_fast(client, state)
        elif gs == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            if is_boss_blind_select(state):
                return state
            client.call("select")
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)

    raise TimeoutError("Could not reach boss blind select in 50 iterations")


def force_boss(client: BalatroClient, boss_name: str) -> bool:
    """Force the boss blind via set API. Returns True if successful."""
    state = client.call("gamestate")
    if get_boss_name(state) == boss_name:
        print(f"    Boss already {boss_name}")
        return True

    boss_key = BOSS_KEYS.get(boss_name)
    if not boss_key:
        print(f"    ERROR: No key mapping for '{boss_name}'")
        return False

    print(f"    Current boss: {get_boss_name(state)}, forcing: {boss_name} ({boss_key})")

    try:
        client.call("set", {"blind": boss_key})
    except APIError as e:
        print(f"    set blind failed: {e.message}")
        return False

    time.sleep(0.3)
    state = client.call("gamestate")
    actual = get_boss_name(state)
    if actual == boss_name:
        print(f"    Forced {boss_name}")
        return True
    print(f"    Force failed: got {actual}")
    return False


# ---------------------------------------------------------------------------
# Injection helpers
# ---------------------------------------------------------------------------

def set_ante(client: BalatroClient, ante: int) -> None:
    try:
        client.call("set", {"ante": ante})
    except APIError as e:
        print(f"    WARNING: set ante failed: {e.message}")


def inject_jokers(client: BalatroClient, joker_keys: list[str]) -> list[str]:
    """Add jokers, selling any existing ones first. Returns list of added keys."""
    state = client.call("gamestate")
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    added = []
    for jk in joker_keys:
        try:
            client.call("add", {"key": jk})
            added.append(jk)
        except APIError as e:
            print(f"    FAILED adding {jk}: {e.message}")
    print(f"    Jokers: {added}")
    return added


def inject_god_mode(client: BalatroClient, *,
                    power_jokers: list[str] | None = None,
                    pluto_levels: int = 30) -> None:
    """Inject overpowered build: level up High Card + add power jokers.

    power_jokers defaults to Photograph, Triboulet, Smiley, Scary Face, Pareidolia.
    """
    if power_jokers is None:
        power_jokers = [
            "j_photograph", "j_triboulet", "j_smiley",
            "j_scary_face", "j_pareidolia",
        ]

    leveled = 0
    for _ in range(pluto_levels):
        try:
            client.call("add", {"key": "c_pluto"})
            client.call("use", {"consumable": 0})
            leveled += 1
        except APIError:
            break
    print(f"    Leveled High Card {leveled} times")

    for jk in power_jokers:
        try:
            client.call("add", {"key": jk})
            print(f"    Added power joker: {jk}")
        except APIError as e:
            print(f"    FAILED adding {jk}: {e.message}")


def inject_milk_trigger(client: BalatroClient) -> None:
    """Add Green Joker (milk_priority=3) to force milking behavior."""
    try:
        client.call("add", {"key": "j_green_joker"})
        print(f"    Added milk trigger: j_green_joker")
    except APIError as e:
        print(f"    FAILED adding j_green_joker: {e.message}")


def burn_discards(client: BalatroClient, target_discards: int = 20) -> None:
    """Discard 5 cards repeatedly to advance discard-tracking jokers (e.g. Yorick).

    Cheats in extra discards, then burns them. target_discards = total cards to discard.
    """
    rounds_needed = target_discards // 5
    try:
        client.call("set", {"discards": rounds_needed + 1})
    except APIError as e:
        print(f"    WARNING: set discards failed: {e.message}")
        return
    discarded = 0
    for _ in range(rounds_needed):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs != "SELECTING_HAND":
            break
        hand_cards = state.get("hand", {}).get("cards", [])
        n = min(5, len(hand_cards))
        if n == 0:
            break
        try:
            client.call("discard", {"cards": list(range(n))})
            discarded += n
        except APIError:
            break
        time.sleep(0.15)
    print(f"    Burned {discarded} cards via discard")


def snapshot_jokers(state: dict, track_keys: set[str]) -> dict:
    """Snapshot ability dicts for tracked joker keys."""
    snap = {}
    for jk in state.get("jokers", {}).get("cards", []):
        key = jk.get("key", "")
        if key in track_keys:
            snap[key] = dict(jk.get("ability", {}))
    return snap
