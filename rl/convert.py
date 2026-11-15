"""Turn the bot's simulator replays into training examples.

    .venv-rl\\Scripts\\python.exe -m rl.convert                 (every replay under bot_log)
    .venv-rl\\Scripts\\python.exe -m rl.convert --cross-check   (also compare with the bot interface)

Each replay is run again on a plain ``jackdaw.core.Game``. Before every bot action
one example is written: what the model sees there (``rl.features.view_of``) and the
action the bot took, in Jackdaw's factored form. Games from exploring instances are
skipped, and so is everything after a win (the bot plays on into endless mode).

How a recorded request becomes an action:

- A hand reorder followed by a play of the first cards (``bot.py`` puts the cards it
  plays in scoring order, then plays ``[0..k-1]``) becomes one play of the cards the
  bot chose, seen from before the reorder. The reorder is kept as the example's
  ``pre`` moves so the game can still be replayed exactly.
- Any other reorder (jokers; the hand before Death) becomes one example per single
  swap, up to the environment's cap on reorder moves in a row.
- Against Cerulean Bell the bot interface selects the forced card first and then the
  request's cards until five are selected.

Every game is checked as it is converted: each action is legal under the stored
mask, and the game ends at the recorded ante, round and money.
"""

from __future__ import annotations

import argparse
import collections
import glob
import multiprocessing
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from balatro_bot.replays import read_replays  # noqa: E402
from jackdaw.bridge.backend import DECK_FROM_BOT, STAKE_FROM_BOT  # noqa: E402
from jackdaw.core import Game  # noqa: E402
from jackdaw.engine import actions as A  # noqa: E402
from jackdaw.env.action_space import (  # noqa: E402
    ActionType,
    engine_action_to_factored,
    get_action_mask,
)
from jackdaw.env.game_interface import game_state  # noqa: E402
from jackdaw.env.game_spec import FactoredAction  # noqa: E402

from rl import dataset  # noqa: E402
from rl.features import MAX_FREE_MOVES, View, view_of  # noqa: E402

DATA = REPO / "rl" / "data"
GAMES_PER_SHARD = 200
MAX_PLAY = 5


class ConvertError(Exception):
    """A replay that does not convert: the reason, with where in the game."""


@dataclass
class Example:
    view: View
    action: FactoredAction
    exec_cards: tuple[int, ...] | None = None
    pre: list[int] = field(default_factory=list)
    flags: int = 0


def left_moves(perm: list[int]) -> list[int]:
    """The single "move one place left" swaps that turn the order ``[0..n-1]`` into
    ``perm`` (position ``j`` ends up holding the old item ``perm[j]``): each entry is
    the position of the item that moves left."""
    order = list(range(len(perm)))
    moves = []
    for j, want in enumerate(perm):
        at = order.index(want)
        while at > j:
            moves.append(at)
            order[at - 1], order[at] = order[at], order[at - 1]
            at -= 1
    return moves


def selection(cards, forced: int | None, limit: int | None = None) -> tuple[int, ...]:
    """The cards a request selects: the forced card first, then the request's cards in
    order, up to ``limit``."""
    picked = list(cards)
    if forced is not None:
        picked = [forced] + [c for c in picked if c != forced]
    if limit is not None:
        picked = picked[:limit]
    return tuple(sorted(picked))


def to_engine(method: str, p: dict, forced: int | None):
    """The engine action a recorded request performs (every request but ``rearrange``)."""
    if method == "select":
        return A.SelectBlind()
    if method == "skip":
        return A.SkipBlind()
    if method == "cash_out":
        return A.CashOut()
    if method == "next_round":
        return A.NextRound()
    if method == "reroll":
        return A.Reroll()
    if method == "play":
        return A.PlayHand(selection(p["cards"], forced, MAX_PLAY))
    if method == "discard":
        return A.Discard(selection(p["cards"], forced, MAX_PLAY))
    if method == "buy":
        if "card" in p:
            return A.BuyCard(p["card"])
        if "pack" in p:
            return A.OpenBooster(p["pack"])
        return A.RedeemVoucher(p["voucher"])
    if method == "pack":
        if p.get("skip"):
            return A.SkipPack()
        targets = p.get("targets")
        return A.PickPackCard(
            p["card"], None if targets is None else selection(targets, forced)
        )
    if method == "use":
        cards = p.get("cards")
        return A.UseConsumable(
            p["consumable"], None if cards is None else selection(cards, forced)
        )
    if method == "sell":
        if "joker" in p:
            return A.SellCard("jokers", p["joker"])
        return A.SellCard("consumables", p["consumable"])
    raise ConvertError(f"no engine action for request {method!r}")


def check_legal(view: View, action: FactoredAction) -> None:
    """Raise unless the action is legal under the view's mask."""
    mask = view.mask
    t = action.action_type
    name = ActionType(t).name
    if not mask.type_mask[t]:
        raise ConvertError(f"{name} is not in the type mask")
    if action.entity_target is not None:
        entities = mask.entity_masks.get(ActionType(t))
        if entities is None or action.entity_target >= len(entities):
            raise ConvertError(f"{name} has no item {action.entity_target}")
        if not entities[action.entity_target]:
            raise ConvertError(f"{name} item {action.entity_target} is masked")
    bounds = view.card_bounds(t, action.entity_target)
    cards = action.card_target
    if cards is None:
        if t in (ActionType.PlayHand, ActionType.Discard):
            raise ConvertError(f"{name} without cards")
        return
    if bounds is None:
        raise ConvertError(f"{name} takes no cards but has {cards}")
    if not bounds[0] <= len(cards) <= bounds[1]:
        raise ConvertError(f"{name} selects {len(cards)} cards, bounds {bounds}")
    selectable = view.selectable(t, action.entity_target)
    if any(c >= len(selectable) or not selectable[c] for c in cards):
        raise ConvertError(f"{name} selects a card it may not: {cards}")
    if mask.forced_card is not None and mask.forced_card not in cards:
        raise ConvertError(f"{name} leaves out the forced card: {cards}")


def new_game(record: dict) -> Game:
    start = record["actions"][0]
    if start[0] != "start":
        raise ConvertError(f"replay starts with {start[0]!r}")
    s = start[1]
    return Game.new(DECK_FROM_BOT[s["deck"]], STAKE_FROM_BOT[s["stake"]], s["seed"])


class _Converter:
    """One replay being converted: the game, and the examples so far."""

    def __init__(self, record: dict, cross_check: bool) -> None:
        self.record = record
        self.acts = record["actions"]
        self.game = new_game(record)
        self.sim = None
        if cross_check:
            from jackdaw.bridge import SimBackend

            self.sim = SimBackend()
            self.sim.handle("menu", None)
            self.sim.handle(*self.acts[0])
        self.examples: list[Example] = []
        self.stats: collections.Counter = collections.Counter()
        self.free_run = 0  # reorder examples in a row, as the environment counts them
        self.pending: list[int] = []  # reorder moves past the cap, before the next example
        self.held: View | None = None  # the view from before the first pending move
        self.won = False

    def view(self) -> View:
        return self.held or view_of(self.game, self.free_run < MAX_FREE_MOVES)

    def emit(self, action: FactoredAction, view: View | None = None, *, pre=(), flags=0,
             exec_cards=None) -> None:
        view = view or self.view()
        if self.pending:
            flags |= dataset.CAPPED
        else:
            check_legal(view, action)
        if view.mask.forced_card is not None:
            flags |= dataset.BELL
        self.examples.append(
            Example(view, action, exec_cards, self.pending + list(pre), flags)
        )
        self.held, self.pending = None, []

    def reorder(self, area: str, moves: list[int]) -> None:
        """A reorder as single swaps: examples up to the cap, unseen moves past it."""
        swap, code = ((A.SwapJokersLeft, dataset.PRE_JOKER_LEFT) if area == "jokers"
                      else (A.SwapHandLeft, dataset.PRE_HAND_LEFT))
        for at in moves:
            action = swap(at)
            if not self.won:
                if self.free_run < MAX_FREE_MOVES and not self.pending:
                    self.emit(engine_action_to_factored(action))
                    self.free_run += 1
                    self.stats[area + "_swap_examples"] += 1
                else:
                    if self.held is None:
                        self.held = view_of(self.game, False)
                    self.pending.append(code + at)
                    self.stats["moves_past_cap"] += 1
            self.game.step(action)

    def folded_play(self, perm: list[int], k: int) -> None:
        """A hand reorder and the play of its first ``k`` cards, as one play."""
        game, stats = self.game, self.stats
        moves = left_moves(perm)
        view = alt = None
        if not self.won:
            view = self.view()
            alt = game.clone()
        for at in moves:
            game.step(A.SwapHandLeft(at))
        played = selection(range(k), get_action_mask(game).forced_card, MAX_PLAY)
        chosen = tuple(sorted(perm[j] for j in played))
        game.step(A.PlayHand(played))
        if self.won:
            return
        alt.step(A.PlayHand(chosen))
        flags = dataset.FOLDED
        real, other = game.last_score, alt.last_score
        stats["folded"] += 1
        stats["folded_score_bot_order"] += real.total
        stats["folded_score_hand_order"] += other.total
        if (real.total, game.dollars, game.phase) != (other.total, alt.dollars, alt.phase):
            flags |= dataset.ORDER_MATTERS
            stats["order_matters"] += 1
            stats["order_matters_score_bot_order"] += real.total
            stats["order_matters_score_hand_order"] += other.total
        self.emit(
            FactoredAction(int(ActionType.PlayHand), card_target=chosen),
            view,
            pre=[dataset.PRE_HAND_LEFT + at for at in moves],
            flags=flags,
            exec_cards=played,
        )
        self.free_run = 0

    def request(self, i: int) -> int:
        """Perform the recorded request at ``i``; returns how many requests it used."""
        method, p = self.acts[i]
        game = self.game
        if method == "rearrange" and "jokers" in p:
            before = [c.sort_id for c in game.jokers]
            self.reorder("jokers", left_moves(p["jokers"]))
            if [c.sort_id for c in game.jokers] != [before[k] for k in p["jokers"]]:
                raise ConvertError("joker reorder did not give the recorded order")
            return 1
        if method == "rearrange":
            nxt = self.acts[i + 1] if i + 1 < len(self.acts) else ("", {})
            k = len(nxt[1].get("cards", ()))
            if nxt[0] == "play" and nxt[1]["cards"] == list(range(k)):
                self.folded_play(p["hand"], k)
                return 2
            self.reorder("hand", left_moves(p["hand"]))
            return 1
        forced = None
        if method in ("play", "discard", "use", "pack"):
            forced = get_action_mask(game).forced_card
        action = to_engine(method, p, forced)
        if not self.won:
            self.emit(engine_action_to_factored(action))
            self.free_run = 0
        game.step(action)
        return 1

    def run(self) -> None:
        acts, game = self.acts, self.game
        i = 1
        try:
            while i < len(acts):
                used = self.request(i)
                if self.sim is not None:
                    for request in acts[i:i + used]:
                        self.sim.handle(*request)
                    if self.sim.game.state_hash() != game.state_hash():
                        raise ConvertError("state differs from the bot interface's")
                i += used
                if not self.won and game_state(game).won:
                    self.won = True
                    self.stats["dropped_after_win"] += len(acts) - i
        except ConvertError as e:
            raise ConvertError(f"seed {self.record['seed']} action {i} {acts[i]}: {e}") from None
        except Exception as e:  # an engine refusal or a malformed request
            raise ConvertError(
                f"seed {self.record['seed']} action {i} {acts[i]}: {type(e).__name__}: {e}"
            ) from None


def convert_game(record: dict, *, cross_check: bool = False):
    """One replay as examples: ``(examples, stats)``. Raises :class:`ConvertError` when
    the replay cannot be converted or does not end where the bot's game did.
    ``cross_check`` also runs the replay through the bot interface and compares the
    game's state after every request."""
    c = _Converter(record, cross_check)
    c.run()
    game, stats = c.game, c.stats
    final = {"ante": game.ante, "round": game.round, "money": game.dollars}
    over = game_state(game).done
    if not over or final != record["final"]:
        raise ConvertError(
            f"seed {record['seed']}: ended at {final} (over: {over}), "
            f"recorded {record['final']}"
        )
    if c.won != bool(record["won"]):
        raise ConvertError(f"seed {record['seed']}: won is {c.won}, recorded {record['won']}")
    stats["games"] += 1
    stats["won"] += c.won
    stats["examples"] += len(c.examples)
    stats["capped_examples"] += sum(1 for e in c.examples if e.flags & dataset.CAPPED)
    stats["bell_examples"] += sum(1 for e in c.examples if e.flags & dataset.BELL)
    return c.examples, stats


def usable_records(paths) -> list[tuple[str, dict]]:
    """Every non-exploring replay in the files, one per seed, as ``(source, record)``."""
    seen: set[str] = set()
    out = []
    for path in sorted(str(p) for p in paths):
        for record in read_replays(path):
            if "explore_rate" in record or record["seed"] in seen:
                continue
            seen.add(record["seed"])
            out.append((Path(path).parent.name + "/" + Path(path).name, record))
    return out


def convert_batch(job) -> tuple[collections.Counter, list[str]]:
    """Convert one shard's worth of replays; save them when the job names a file."""
    path, items, cross_check = job
    writer = dataset.ShardWriter()
    total: collections.Counter = collections.Counter()
    errors = []
    for source, record in items:
        try:
            examples, stats = convert_game(record, cross_check=cross_check)
        except ConvertError as e:
            errors.append(str(e))
            continue
        total.update(stats)
        if path is None:
            continue
        start = record["actions"][0][1]
        writer.start_game(
            record["seed"], source, DECK_FROM_BOT[start["deck"]], STAKE_FROM_BOT[start["stake"]]
        )
        for ex in examples:
            writer.add(ex.view, ex.action, exec_cards=ex.exec_cards, pre=ex.pre, flags=ex.flags)
        writer.end_game(record["won"], record["final"])
    if path is not None and writer.n_games:
        writer.save(path)
    return total, errors


def converted_seeds(shard_dir: Path) -> set[str]:
    import numpy as np

    seeds: set[str] = set()
    for path in sorted(shard_dir.glob("shard_*.npz")):
        with np.load(path) as z:
            seeds.update(str(s) for s in z["g_seed"])
    return seeds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("replays", nargs="*", help="replay files (default: bot_log/*/replay_*.jsonl.gz)")
    ap.add_argument("--out", default=str(DATA / "shards"))
    ap.add_argument("--cross-check", action="store_true",
                    help="also run each game through the bot interface and compare every state")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="convert at most this many games")
    ap.add_argument("--check-only", action="store_true", help="convert and check, save nothing")
    args = ap.parse_args()

    paths = args.replays or glob.glob(str(REPO / "bot_log" / "*" / "replay_*.jsonl.gz"))
    out = Path(args.out)
    have = sorted(out.glob("shard_*.npz")) if out.exists() else []
    done = set() if args.check_only else converted_seeds(out)
    items = [(s, r) for s, r in usable_records(paths) if r["seed"] not in done]
    items.sort(key=lambda sr: sr[1]["seed"])
    if args.limit:
        items = items[:args.limit]
    jobs = []
    for n, at in enumerate(range(0, len(items), GAMES_PER_SHARD)):
        path = None if args.check_only else out / f"shard_{len(have) + n:04d}.npz"
        jobs.append((path, items[at:at + GAMES_PER_SHARD], args.cross_check))
    print(f"{len(items)} games to convert ({len(done)} already converted), {len(jobs)} shards")
    started = time.perf_counter()
    total: collections.Counter = collections.Counter()
    errors: list[str] = []
    if jobs:
        with multiprocessing.Pool(min(args.workers, len(jobs))) as pool:
            for stats, errs in pool.imap_unordered(convert_batch, jobs):
                total.update(stats)
                errors.extend(errs)
    print(f"converted {total['games']} games in {time.perf_counter() - started:.1f}s")
    for key in sorted(total):
        print(f"  {key}: {total[key]:,.0f}")
    for e in errors:
        print("FAILED", e)
    print(f"{len(errors)} games failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
