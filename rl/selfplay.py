"""Games a policy plays by itself, recorded step by step for learning.

Many games run side by side. At each tick every game takes one step: the policy
samples an action for each (``rl.model.act`` with a generator), plays come out in the
bot's order (``rl.ordering``), and the step's points are counted.

Points: a third of a point per blind beaten or skipped (a whole ante earns one) and the
win bonus (``WIN_BONUS`` unless a run sets its own) more for the win, so a game that stops
at its win is worth about its final ante minus one, plus the bonus when won. With an
endless bonus a won game plays on into endless mode, each blind beaten or skipped there
earning a third of the bonus. With ``overkill`` above 0, a beaten blind also earns
``overkill`` times the natural log of its score over target, the log capped at
``OVERKILL_CAP``: a small, ever-growing pull toward bigger engines that never outweighs
the blind itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
import torch

from jackdaw.env.action_space import ActionType
from jackdaw.env.rewards import Transition

from rl import dataset
from rl.evaluate import DECK, MAX_STEPS, STAKE, WIN_ANTE
from rl.model import Policy, Prepared, act
from rl.ordering import put_in_order
from rl.playback import env_view, make_env, no_legal_action

WIN_BONUS = 8.0
ENDLESS_BONUS = 0.0
"""Points per endless ante; 0 ends each game at its win."""
OVERKILL = 0.0
OVERKILL_CAP = 24.0
"""The most the log of a blind's score over target counts for (endless scores reach
infinity)."""


def points(t: Transition, win_bonus: float = WIN_BONUS, endless_bonus: float = ENDLESS_BONUS,
           overkill: float = OVERKILL) -> float:
    """The points one step earns: before the win, a third per blind beaten or skipped and
    the win bonus on the step that wins; after it, a third of ``endless_bonus`` per blind
    beaten or skipped. A beaten blind adds ``overkill`` times the log of its score over
    target."""
    passed = t.blind_beaten or t.action.action_type == ActionType.SkipBlind
    earned = (endless_bonus if t.prev.won else 1.0) / 3 if passed else 0.0
    if not t.prev.won and t.run_won:
        earned += win_bonus
    if overkill and t.blind_beaten:
        earned += overkill * overkill_log(t.curr.chips, t.prev.blind_chips)
    return earned


def overkill_log(chips: float, target: float) -> float:
    """The natural log of a beaten blind's score over its target, from 0 (a blind only
    just beaten, or saved by Mr. Bones) to ``OVERKILL_CAP``."""
    if not target > 0 or not chips > target:
        return 0.0
    return min(math.log(chips / target), OVERKILL_CAP)


def train_seed(run: str, n: int) -> str:
    """The ``n``-th training seed of a run. The dash keeps it apart from the game's own
    8-character seeds; the prefix from the evaluation seeds."""
    return f"RLT-{run}-{n:07d}"


@dataclass
class Rollout:
    """The steps of one stretch of play, ``ticks`` x ``games``, in tick order: step
    ``k`` is game slot ``k % games`` at tick ``k // games``."""

    examples: dataset.Examples
    logp: np.ndarray
    value: np.ndarray
    reward: np.ndarray
    done: np.ndarray
    """The game ended on this step (the slot's next step starts a new game)."""
    last_value: np.ndarray
    """Per slot, the value of the state play stopped at (a fresh game's start where the
    last tick ended one; ``done`` makes it count for nothing then)."""
    games: int
    finished: list[dict] = field(default_factory=list)


@dataclass
class Games:
    """Whole games, every step in play order per game."""

    examples: dataset.Examples
    reward: np.ndarray
    game: np.ndarray
    """Per step, the game it belongs to (an index into ``results``)."""
    results: list[dict]
    logp: np.ndarray
    """Per step, the log-probability of the move the policy drew."""

    def points_to_go(self) -> np.ndarray:
        """Per step, the points the rest of its game brought."""
        out = np.zeros(len(self.reward), np.float32)
        for g in range(len(self.results)):
            steps = np.flatnonzero(self.game == g)
            out[steps] = np.cumsum(self.reward[steps][::-1])[::-1]
        return out


class SelfPlay:
    """Games on the training seeds ``train_seed(run, first), train_seed(run, first+1), ...``."""

    def __init__(self, run: str, *, seed: int = 0, first: int = 0,
                 max_steps: int = MAX_STEPS, deck: str = DECK, stake: int = STAKE,
                 win_bonus: float = WIN_BONUS, endless_bonus: float = ENDLESS_BONUS,
                 overkill: float = OVERKILL) -> None:
        self.run = run
        self.overkill = overkill
        self.max_steps = max_steps
        self.deck, self.stake, self.win_bonus = deck, stake, win_bonus
        self.endless_bonus = endless_bonus
        self.next_seed = first
        self.generator = torch.Generator().manual_seed(seed)
        self.slots: list[dict] = []

    def _new_game(self, seed: str | None = None) -> dict:
        """A game on ``seed``, or on the next training seed."""
        if seed is None:
            seed = train_seed(self.run, self.next_seed)
            self.next_seed += 1
        env = make_env(self.deck, self.stake, on_refusal="truncate", max_steps=self.max_steps,
                       end_on_win=not self.endless_bonus)
        _, _, info = env.reset(seed=seed)
        return {"seed": seed, "env": env, "info": info, "steps": 0, "points": 0.0}

    def _tick(self, model: Policy, writer: dataset.ShardWriter):
        """Every slot takes one sampled step. Per slot: the action's log-probability,
        the value, the points, and the game's result if it ended (else ``None``)."""
        views = [env_view(slot["info"]) for slot in self.slots]
        actions, logp, value = act(model, Prepared(dataset.examples_of(views)),
                                   generator=self.generator)
        reward = np.zeros(len(self.slots), np.float32)
        ended: list[dict | None] = []
        for k, (slot, view, action) in enumerate(zip(self.slots, views, actions)):
            writer.add(view, action)
            played = action
            if action.action_type == ActionType.PlayHand:
                played = replace(
                    action, card_target=put_in_order(slot["info"]["game"], action.card_target))
            _, terminated, truncated, _, info = slot["env"].step(played)
            reward[k] = points(Transition.from_step(played, terminated, truncated, info),
                               self.win_bonus, self.endless_bonus, self.overkill)
            slot["info"] = info
            slot["steps"] += 1
            slot["points"] += float(reward[k])
            stuck = not (terminated or truncated) and no_legal_action(info)
            ended.append(_result(slot, truncated or stuck, info, stuck)
                         if terminated or truncated or stuck else None)
        return logp, value, reward, ended

    def collect(self, model: Policy, games: int, ticks: int) -> Rollout:
        """``games`` slots each take ``ticks`` steps; a game that ends is replaced at
        once by one on the next seed. Slots carry over from one call to the next."""
        while len(self.slots) < games:
            self.slots.append(self._new_game())
        n = len(self.slots)
        writer = dataset.ShardWriter()
        writer.start_game("", "", self.deck, self.stake)
        logp = np.zeros((ticks, n), np.float32)
        value = np.zeros((ticks, n), np.float32)
        reward = np.zeros((ticks, n), np.float32)
        done = np.zeros((ticks, n), bool)
        finished: list[dict] = []
        model.eval()
        for tick in range(ticks):
            logp[tick], value[tick], reward[tick], ended = self._tick(model, writer)
            for k, result in enumerate(ended):
                if result is not None:
                    done[tick, k] = True
                    finished.append(result)
                    self.slots[k] = self._new_game()
        writer.end_game(False, {"ante": 0, "round": 0, "money": 0})
        views = [env_view(slot["info"]) for slot in self.slots]
        _, _, last = act(model, Prepared(dataset.examples_of(views)))
        return Rollout(
            examples=dataset.Examples(writer.arrays()), logp=logp.reshape(-1),
            value=value.reshape(-1), reward=reward.reshape(-1), done=done.reshape(-1),
            last_value=last, games=n, finished=finished,
        )

    def whole_games(self, model: Policy, total: int, parallel: int = 64,
                    repeat: int = 1) -> Games:
        """Play ``total`` new games to their end, ``parallel`` at a time. With
        ``repeat`` above 1, each new seed is played that many times in a row (games
        ``k * repeat`` to ``k * repeat + repeat - 1`` share a seed); only the policy's
        draws differ between them."""
        writer = dataset.ShardWriter()
        writer.start_game("", "", self.deck, self.stake)
        self.slots = []
        game_of_slot: list[int] = []
        rewards: list[np.ndarray] = []
        logps: list[np.ndarray] = []
        games: list[np.ndarray] = []
        results: list[dict | None] = []
        started = 0
        seed = None
        model.eval()
        while started < total or self.slots:
            while started < total and len(self.slots) < parallel:
                if started % repeat == 0:
                    seed = train_seed(self.run, self.next_seed)
                    self.next_seed += 1
                self.slots.append(self._new_game(seed))
                game_of_slot.append(started)
                results.append(None)
                started += 1
            logp, _, reward, ended = self._tick(model, writer)
            rewards.append(reward)
            logps.append(logp)
            games.append(np.asarray(game_of_slot))
            keep = [k for k, result in enumerate(ended) if result is None]
            for k, result in enumerate(ended):
                if result is not None:
                    results[game_of_slot[k]] = result
            self.slots = [self.slots[k] for k in keep]
            game_of_slot = [game_of_slot[k] for k in keep]
        writer.end_game(False, {"ante": 0, "round": 0, "money": 0})
        return Games(dataset.Examples(writer.arrays()), np.concatenate(rewards),
                     np.concatenate(games), results, np.concatenate(logps))


def _result(slot: dict, truncated: bool, info: dict, stuck: bool = False) -> dict:
    """A game's end. A won game cut short in endless mode keeps its win and the points it
    earned there."""
    state = info["state"]
    won = bool(state.won)
    cut = None
    if stuck and not won:
        cut = "no legal action"
    elif truncated and not won:
        cut = next((f"{k}: {info[k]}" for k in ("refused", "not_ported", "vetoed") if k in info),
                   "step limit")
    return {"seed": slot["seed"], "won": won,
            "ante": WIN_ANTE if won else min(int(state.ante), WIN_ANTE),
            "steps": slot["steps"], "points": slot["points"], "cut": cut}


def same_seed_scores(games: Games, repeat: int, penalty: np.ndarray | None = None):
    """Per step, how its game did against the other plays of the same seed: the game's
    points (less ``penalty`` summed over its steps) minus the average of the others.
    Also each game's points less penalty."""
    n = len(games.results)
    if repeat < 2 or n % repeat:
        raise ValueError(f"{n} games cannot be split into groups of {repeat} plays")
    cost = games.reward if penalty is None else games.reward - penalty
    total = np.bincount(games.game, weights=cost, minlength=n)
    groups = total.reshape(-1, repeat)
    others = (groups.sum(1, keepdims=True) - groups) / (repeat - 1)
    return (groups - others).reshape(-1)[games.game].astype(np.float32), total


def advantages(r: Rollout, lam: float = 0.95) -> tuple[np.ndarray, np.ndarray]:
    """Per step, how much better the game went than the value expected (smoothed over
    the following steps by ``lam``), and the points that followed as the value's target.
    No discount within a game; a game that ended is followed by nothing."""
    n = r.games
    ticks = len(r.reward) // n
    reward = r.reward.reshape(ticks, n)
    value = r.value.reshape(ticks, n)
    done = r.done.reshape(ticks, n)
    adv = np.zeros((ticks, n), np.float32)
    running = np.zeros(n, np.float32)
    following = r.last_value.astype(np.float32)
    for tick in range(ticks - 1, -1, -1):
        alive = ~done[tick]
        delta = reward[tick] + np.where(alive, following, 0.0) - value[tick]
        running = delta + lam * np.where(alive, running, 0.0)
        adv[tick] = running
        following = value[tick]
    return adv.reshape(-1), (adv + value).reshape(-1)
