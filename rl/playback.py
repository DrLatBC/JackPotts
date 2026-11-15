"""The no-model test: play converted games back through Jackdaw's environment.

    .venv-rl\\Scripts\\python.exe -m rl.playback            (every converted game)
    .venv-rl\\Scripts\\python.exe -m rl.playback --heldout  (the held-out games only)

Each game's stored actions are fed to ``jackdaw.env.BalatroEnvironment`` the way a
model's would be. At every step the environment must show exactly the observation
and mask the example stored, and the game must end where the bot's did (or, for a
game the bot won, be won on the last stored step). That proves the examples, the
reset, the masks, the shop rows and the card targets line up with the environment
before any model exists.

The one thing a model cannot do is the bot's hand reorder before a play. Those moves
(an example's ``pre``) are applied to the environment's game directly, between the
check of what the environment shows and the step.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

from jackdaw.engine import actions as A
from jackdaw.env.balatro_env import BalatroEnvironment
from jackdaw.env.game_interface import DirectAdapter

from rl import dataset
from rl.dataset import Examples
from rl.features import ENTITY_TYPES, View, view_from

DATA = Path(__file__).resolve().parent / "data"


def make_env(deck: str, stake: int, *, on_refusal: str = "raise", **kw) -> BalatroEnvironment:
    """The environment a model plays in: the engine in process, one deck and stake,
    an episode that ends on a win."""
    return BalatroEnvironment(
        DirectAdapter, back_keys=[deck], stakes=[stake], on_refusal=on_refusal, **kw
    )


def env_view(info: dict) -> View:
    """The view of the decision point an environment step or reset returned."""
    return view_from(
        info["game"], info["observation"], info["action_mask"], info["state"],
        info["shop_splits"],
    )


def difference(view: View, ex: Examples, i: int) -> str | None:
    """What the environment shows differently from stored example ``i``, if anything."""
    obs, mask = view.obs, view.mask
    pairs = [
        ("global", obs.global_context, ex["glob"][i]),
        ("hand", obs.hand_cards, ex.rows("hand", i)),
        ("jokers", obs.jokers, ex.rows("joker", i)),
        ("consumables", obs.consumables, ex.rows("cons", i)),
        ("shop", obs.shop_cards, ex.rows("shop", i)),
        ("pack", obs.pack_cards, ex.rows("pack", i)),
        ("consumable targets", view.cons_extra, ex.rows("cons_extra", i)),
        ("pack extras", view.pack_extra, ex.rows("pack_extra", i)),
        ("shop splits", np.asarray(view.splits), ex["splits"][i]),
        ("type mask", mask.type_mask, ex["type_mask"][i]),
        ("card mask", mask.card_mask, ex.rows("card_mask", i)),
        ("selection bounds", np.asarray([mask.min_card_select, mask.max_card_select]),
         np.asarray([ex["min_sel"][i], ex["max_sel"][i]])),
        ("forced card", np.asarray(-1 if mask.forced_card is None else mask.forced_card),
         np.asarray(ex["forced"][i])),
    ]
    for t in ENTITY_TYPES:
        shown = mask.entity_masks.get(t)
        pairs.append((f"{t.name} mask", np.zeros(0, bool) if shown is None else shown,
                      ex.entity_mask(i, t)))
    for name, shown, stored in pairs:
        if shown.shape != stored.shape or not np.array_equal(shown, stored):
            return name
    return None


def apply_pre(game, moves) -> None:
    """Apply an example's unseen reorder moves to the game."""
    for code in moves:
        code = int(code)
        if code >= dataset.PRE_HAND_LEFT:
            game.step(A.SwapHandLeft(code - dataset.PRE_HAND_LEFT))
        else:
            game.step(A.SwapJokersLeft(code - dataset.PRE_JOKER_LEFT))


def play_back(ex: Examples, g: int) -> str | None:
    """Play game ``g`` of the examples back through the environment. Returns what went
    wrong, or ``None`` when every step matched and the game ended where it should."""
    seed = str(ex["g_seed"][g])
    env = make_env(str(ex["g_deck"][g]), int(ex["g_stake"][g]))
    _, _, info = env.reset(seed=seed)
    steps = ex.game_examples(g)
    terminated = truncated = False
    for n, i in enumerate(steps):
        if terminated or truncated:
            return f"step {n}: the episode ended before the stored actions ran out"
        diff = difference(env_view(info), ex, i)
        if diff is not None:
            return f"step {n}: the environment's {diff} differs from the stored example"
        apply_pre(info["game"], ex.rows("pre", i))
        try:
            _, terminated, truncated, _, info = env.step(ex.exec_action(i))
        except Exception as e:  # the engine refused the stored action
            return f"step {n}: {type(e).__name__}: {e}"
    state = info["state"]
    if truncated or not terminated:
        return f"the episode did not end with the stored actions (truncated: {truncated})"
    if bool(ex["g_won"][g]):
        return None if state.won else "the bot won this game; the playback did not"
    ended = (state.ante, state.round, state.dollars)
    recorded = tuple(int(v) for v in ex["g_final"][g])
    if state.won or ended != recorded:
        return f"ended at ante/round/money {ended} (won: {state.won}), recorded {recorded}"
    return None


def read_seeds(path: Path) -> set[str]:
    return {s for s in path.read_text().split() if s}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shards", default=str(DATA / "shards"))
    ap.add_argument("--heldout", action="store_true", help="only the held-out games")
    args = ap.parse_args()
    keep = read_seeds(DATA / "heldout_seeds.txt") if args.heldout else None
    games = steps = 0
    failures = []
    started = time.perf_counter()
    for path in sorted(Path(args.shards).glob("shard_*.npz")):
        ex = dataset.load_shard(path)
        for g in range(ex.n_games):
            seed = str(ex["g_seed"][g])
            if keep is not None and seed not in keep:
                continue
            games += 1
            steps += len(ex.game_examples(g))
            problem = play_back(ex, g)
            if problem:
                failures.append(f"seed {seed}: {problem}")
    for f in failures:
        print("FAILED", f)
    print(f"{games} games, {steps} steps played back in {time.perf_counter() - started:.1f}s; "
          f"{len(failures)} did not match")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
