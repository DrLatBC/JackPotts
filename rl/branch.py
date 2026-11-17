"""Branch play: judge single decisions by playing the game on both ways.

Main lines play new training seeds, always taking the policy's most likely move. At a
share (``rate``) of their steps the policy draws up to ``draws`` moves looking for one
that is not that move; when one comes up, a copy of the game (``rl.playback.copy_env``)
plays it, then plays on the same way. Both lines play to their game's end, and the
decision is worth the main line's points less the copy's (``rl.selfplay.game_points``).

A copy whose state at a blind select is the main line's state at the same ante and round
has rejoined it: the state holds the game's random streams, so the rest plays out alike.
It stops there, tied. Hashing only at blind selects keeps the check cheap (a hash costs
about as much as a step).

Each decision that did not tie gives two examples at its state: the main line's move with
half the difference as its advantage, the drawn move with minus half. The main-line states
draws were tried at are kept too, with the main line's move, so a learning step can be
measured on states it was not trained on (``rl.ppo.one_step``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import torch

from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction

from rl import dataset
from rl.evaluate import DECK, MAX_STEPS, STAKE, WIN_ANTE
from rl.model import Policy, Prepared, act
from rl.ordering import put_in_order
from rl.playback import copy_env, env_view, make_env, no_legal_action
from rl.selfplay import WIN_BONUS, game_points, train_seed


@dataclass
class Batch:
    """What one call of ``BranchPlay.play`` collected."""

    examples: dataset.Examples
    """Two per untied decision, in pairs: the main line's move, then the drawn move."""
    logp: np.ndarray
    """Per example, the move's log-probability when it was chosen or drawn."""
    adv: np.ndarray
    """Per example, in points: plus half the decision's difference for the main line's
    move, minus half for the drawn one."""
    states: dataset.Examples
    """The main-line states draws were tried at, each with the main line's move."""
    state_logp: np.ndarray
    finished: list[dict]
    """The main lines that ended during the call."""
    decisions: list[dict]
    """Every decision resolved during the call, tied or not: where it was taken, the two
    moves' types and how the lines' results differ."""


def same_move(a: FactoredAction, b: FactoredAction) -> bool:
    """The same move: card order does not matter (plays go out in the bot's order)."""
    return (a.action_type == b.action_type and a.entity_target == b.entity_target
            and sorted(a.card_target or ()) == sorted(b.card_target or ()))


class BranchPlay:
    """Main lines on the training seeds ``train_seed(run, first), ...`` and their branches.
    Lines carry over from one ``play`` call to the next."""

    def __init__(self, run: str, *, seed: int = 0, first: int = 0, deck: str = DECK,
                 stake: int = STAKE, win_bonus: float = WIN_BONUS, rate: float = 0.05,
                 draws: int = 4, mains: int = 64, max_steps: int = MAX_STEPS) -> None:
        self.run, self.deck, self.stake, self.win_bonus = run, deck, stake, win_bonus
        self.rate, self.draws, self.mains, self.max_steps = rate, draws, mains, max_steps
        self.next_seed = first
        self.generator = torch.Generator().manual_seed(seed)
        self.rng = np.random.default_rng(seed)
        self.lines: list[dict] = []
        # Per seed with a live main line or open branches: the main line's result once it
        # ends, its state hashes at blind selects, its open branches, and the decisions
        # waiting for its result.
        self.seeds: dict[str, dict] = {}

    def play(self, model: Policy, decisions: int) -> Batch:
        """Play until ``decisions`` untied decisions have resolved."""
        self._pairs: list[tuple] = []
        self._states: list[tuple] = []
        self._finished: list[dict] = []
        self._resolved: list[dict] = []
        model.eval()
        while len(self._pairs) < decisions:
            self._tick(model)
        pairs, states = self._pairs, self._states
        ex, logp, adv = _pair_examples(pairs, self.deck, self.stake)
        st, st_logp = _examples([(v, a) for v, a, _ in states], self.deck, self.stake), \
            np.array([lp for _, _, lp in states], np.float32)
        return Batch(ex, logp, adv, st, st_logp, self._finished, self._resolved)

    # -- one tick ------------------------------------------------------------------

    def _new_main(self) -> dict:
        seed = train_seed(self.run, self.next_seed)
        self.next_seed += 1
        env = make_env(self.deck, self.stake, on_refusal="truncate", max_steps=self.max_steps)
        _, _, info = env.reset(seed=seed)
        self.seeds[seed] = {"result": None, "hash": {}, "open": 0, "waiting": []}
        return {"role": "main", "seed": seed, "env": env, "info": info, "steps": 0, "force": None}

    def _tick(self, model: Policy) -> None:
        while sum(line["role"] == "main" for line in self.lines) < self.mains:
            self.lines.append(self._new_main())
        views = [env_view(line["info"]) for line in self.lines]
        prep = Prepared(dataset.examples_of(views))
        greedy, glogp, _ = act(model, prep)
        tried = [k for k, line in enumerate(self.lines)
                 if line["role"] == "main" and self.rng.random() < self.rate]
        for k in tried:
            self._states.append((views[k], greedy[k], float(glogp[k])))
        drawn = self._draw(model, prep, tried, greedy)
        spawned, still = [], []
        for k, line in enumerate(self.lines):
            move = greedy[k]
            if line["force"] is not None:
                move, line["force"] = line["force"], None
            if k in drawn:
                alt, alt_logp = drawn[k]
                spawned.append(self._branch(line, views[k], greedy[k], float(glogp[k]), alt, alt_logp))
            if self._step(line, move):
                still.append(line)
        self.lines = [line for line in still if not self._joined(line)] + spawned

    def _draw(self, model: Policy, prep: Prepared, tried: list[int], greedy) -> dict:
        """Per tried slot, a drawn move other than the most likely one, if one came up."""
        found: dict[int, tuple[FactoredAction, float]] = {}
        left = list(tried)
        for _ in range(self.draws):
            if not left:
                break
            moves, logp, _ = act(model, prep, np.asarray(left), generator=self.generator)
            again = []
            for k, move, lp in zip(left, moves, logp):
                if same_move(move, greedy[k]):
                    again.append(k)
                else:
                    found[k] = (move, float(lp))
            left = again
        return found

    def _branch(self, main: dict, view, move, move_logp: float, alt, alt_logp: float) -> dict:
        g = main["info"]["game"]
        env, info = copy_env(main["env"])
        self.seeds[main["seed"]]["open"] += 1
        decision = {"phase": g.phase.value, "ante": int(g.ante), "round": int(g.round),
                    "main move": ActionType(move.action_type).name,
                    "drawn move": ActionType(alt.action_type).name,
                    "pair": (view, move, move_logp, alt, alt_logp)}
        return {"role": "branch", "seed": main["seed"], "env": env, "info": info, "steps": 0,
                "force": alt, "decision": decision, "hashes": {}}

    def _step(self, line: dict, move: FactoredAction) -> bool:
        """Play one move; True while the line goes on."""
        game = line["info"]["game"]
        if move.action_type == ActionType.PlayHand:
            move = replace(move, card_target=put_in_order(game, move.card_target))
        _, terminated, truncated, _, info = line["env"].step(move)
        line["info"] = info
        line["steps"] += 1
        stuck = not (terminated or truncated) and no_legal_action(info)
        if terminated or truncated or stuck:
            self._ended(line, _result(info, truncated or stuck, stuck))
            return False
        g = info["game"]
        if g.phase.value == "blind_select":
            book = self.seeds[line["seed"]]["hash"] if line["role"] == "main" else line["hashes"]
            key = (int(g.ante), int(g.round))
            if key not in book:
                book[key] = g.state_hash()
        return True

    def _joined(self, line: dict) -> bool:
        """A branch back on its main line's path is resolved, tied, and stops."""
        if line["role"] != "branch":
            return False
        main = self.seeds[line["seed"]]["hash"]
        if not any(main.get(key) == h for key, h in line["hashes"].items()):
            return False
        self._resolve(line["decision"], None, None)
        self._close(line["seed"])
        return True

    def _ended(self, line: dict, result: dict) -> None:
        seed = self.seeds[line["seed"]]
        if line["role"] == "main":
            result = {**result, "seed": line["seed"], "steps": line["steps"],
                      "points": game_points(result, self.win_bonus)}
            seed["result"] = result
            self._finished.append(result)
            for decision, branch in seed["waiting"]:
                self._resolve(decision, result, branch)
            seed["waiting"] = []
            self._close(line["seed"], opened=False)
            return
        if seed["result"] is None:
            seed["waiting"].append((line["decision"], result))
        else:
            self._resolve(line["decision"], seed["result"], result)
        self._close(line["seed"])

    def _close(self, seed: str, opened: bool = True) -> None:
        """One of the seed's branches ended (or its main line did, ``opened`` False); the
        seed's bookkeeping goes once nothing of it is left."""
        s = self.seeds[seed]
        if opened:
            s["open"] -= 1
        if s["open"] == 0 and s["result"] is not None:
            del self.seeds[seed]

    def _resolve(self, decision: dict, main: dict | None, branch: dict | None) -> None:
        view, move, move_logp, alt, alt_logp = decision.pop("pair")
        if main is None:
            decision.update({"joined": True, "tied": True, "cut": False, "diff": 0.0,
                             "ante diff": 0, "win diff": 0})
            self._resolved.append(decision)
            return
        cut = main["cut"] or branch["cut"]
        diff = 0.0 if cut else game_points(main, self.win_bonus) - game_points(branch, self.win_bonus)
        decision.update({"joined": False, "tied": not cut and diff == 0.0, "cut": bool(cut),
                         "diff": diff, "ante diff": main["ante"] - branch["ante"],
                         "win diff": int(main["won"]) - int(branch["won"])})
        self._resolved.append(decision)
        if not cut and diff != 0.0:
            self._pairs.append((view, move, move_logp, alt, alt_logp, diff))


def _result(info: dict, truncated: bool, stuck: bool) -> dict:
    state = info["state"]
    cut = None
    if stuck:
        cut = "no legal action"
    elif truncated:
        cut = next((f"{k}: {info[k]}" for k in ("refused", "not_ported", "vetoed") if k in info),
                   "step limit")
    won = bool(state.won) and cut is None
    return {"won": won, "ante": WIN_ANTE if won else min(int(state.ante), WIN_ANTE), "cut": cut}


def _examples(rows, deck: str, stake: int) -> dataset.Examples:
    writer = dataset.ShardWriter()
    writer.start_game("", "", deck, stake)
    for view, move in rows:
        writer.add(view, move)
    writer.end_game(False, {"ante": 0, "round": 0, "money": 0})
    return dataset.Examples(writer.arrays())


def _pair_examples(pairs, deck: str, stake: int):
    rows, logp, adv = [], [], []
    for view, move, move_logp, alt, alt_logp, diff in pairs:
        rows += [(view, move), (view, alt)]
        logp += [move_logp, alt_logp]
        adv += [diff / 2, -diff / 2]
    return (_examples(rows, deck, stake), np.asarray(logp, np.float32),
            np.asarray(adv, np.float32))


def tally(decisions: list[dict]) -> dict:
    """The resolved decisions summed: how many, how many tied, rejoined or cut, and the
    main line's moves against the drawn ones in points, antes and wins; over all of them,
    by phase, and by the pair of move types ("PlayHand > Discard")."""
    out: dict = {}
    for d in decisions:
        for key in ("all", d["phase"], f'{d["main move"]} > {d["drawn move"]}'):
            t = out.setdefault(key, {"n": 0, "tied": 0, "joined": 0, "cut": 0, "main better": 0,
                                     "drawn better": 0, "points": 0.0, "antes": 0, "wins": 0})
            t["n"] += 1
            t["tied"] += d["tied"]
            t["joined"] += d["joined"]
            t["cut"] += d["cut"]
            t["main better"] += d["diff"] > 0
            t["drawn better"] += d["diff"] < 0
            t["points"] += d["diff"]
            t["antes"] += d["ante diff"]
            t["wins"] += d["win diff"]
    return out
