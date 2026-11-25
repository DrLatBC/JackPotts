"""Branch play: judge single decisions by playing the game on both ways.

Main lines play new training seeds, always taking the policy's most likely move. At a
share (``rate``) of their steps the policy draws up to ``draws`` moves looking for one
that is not that move; when one comes up, a copy of the game (``rl.playback.copy_env``)
plays it, then plays on the same way. Both lines play to their game's end (with an endless
bonus, a won game's end in endless mode), and the decision is worth the main line's points
less the copy's: each line adds up its steps' ``rl.selfplay.points``, a copy starting from
its main line's points at the decision.

A copy whose state at a blind select is the main line's state at the same ante and round
has rejoined it: the state holds the game's random streams, so the rest plays out alike.
It stops there, tied. Hashing only at blind selects keeps the check cheap (a hash costs
about as much as a step).

Each decision that did not tie gives two examples at its state: the main line's move with
half the difference as its advantage, the drawn move with minus half. The main-line states
draws were tried at are kept too, each with the first move drawn there (a sample of the
policy, as self-play's moves are), so a learning step can be measured on states it was
not trained on and held near its start there (``rl.ppo.one_step``).

Some moves the bot never makes, so a copy of the bot gives them next to no probability and
draws would never try them: the boss reroll (Director's Cut, Retcon), skipping a blind, and
buying a Planet, Tarot or Spectral to use on the spot. A main line branches on these
outside the rate (``FORCED``): once per occasion (an ante, a blind, a shop visit) at its
first decision point where the move is legal and not the line's own pick, with that
move's chance. A forced move takes the policy's likeliest item. Those branches are not
among the kept states: their moves are not samples of the policy.

Seals come up a few times a game, so draws seldom land on them. A main line holding
sealed cards branches on the move the policy makes with the seals hidden (``SEALED``):
once per blind at a play or discard, once per pack opened. In a pack with a seal
Spectral the policy passes over, the branch takes that Spectral instead. Whichever line
does better teaches the policy what the seal is worth there.

Shop branches (``rl.shop_branches``) try buys and uses the policy under-makes, each once per
shop visit (a held tarot once per blind) at its ``SHOP_CHANCE``: Blueprint or Brainstorm,
Midas Mask beside Vampire, the joker that most raises the average best-hand score, a joker
for an open slot it leaves the shop with, using an enhancing tarot held through a shop
visit, and passing on a planet for a hand the run does not play (that branch may not buy
it this visit). A buy that needs a sale first makes the sale on the branch and then the
buy. Each move after the first is trained too, at its own state on the branch with the
branch's half of the difference: the moves were one plan, so they share its result.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import torch

from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction
from jackdaw.env.rewards import Transition

from rl import dataset, shop_branches
from rl.evaluate import DECK, MAX_STEPS, STAKE, WIN_ANTE
from rl.model import DISCARD, PICK, PLAY, Policy, Prepared, _complete, act, complete
from rl.ordering import put_in_order
from rl.playback import copy_env, env_view, make_env, no_legal_action
from rl.selfplay import ENDLESS_BONUS, WIN_BONUS, points, train_seed
from rl.shop_branches import SHOP_CHANCE

REROLL = int(ActionType.RerollBoss)
SKIP = int(ActionType.SkipBlind)
BUY_AND_USE = int(ActionType.BuyAndUse)
FORCED: dict[int, tuple[str, float]] = {
    REROLL: ("ante", 0.5),
    SKIP: ("blind", 0.01),
    BUY_AND_USE: ("shop", 0.1),
}
"""Per forced move type: the occasion it is tried once in, and the chance it is tried."""

SEAL_SPECTRALS = frozenset({"c_deja_vu", "c_talisman", "c_trance", "c_medium"})
SEALED: dict[str, float] = {"hand": 1.0, "pack": 1.0}
"""The chance a seal branch is tried, once per blind's hand and once per pack opened."""


@dataclass
class Batch:
    """What one call of ``BranchPlay.play`` collected."""

    examples: dataset.Examples
    """Per untied decision: the main line's move, the drawn move, then any further forced
    moves the branch made at its own states."""
    logp: np.ndarray
    """Per example, the move's log-probability when it was chosen or drawn."""
    adv: np.ndarray
    """Per example, in points: plus half the decision's difference for the main line's
    move, minus half for the drawn one and the branch's further forced moves."""
    states: dataset.Examples
    """The main-line states draws were tried at, each with the first move drawn there."""
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
                 draws: int = 4, mains: int = 64, max_steps: int = MAX_STEPS,
                 endless_bonus: float = ENDLESS_BONUS) -> None:
        self.run, self.deck, self.stake, self.win_bonus = run, deck, stake, win_bonus
        self.endless_bonus = endless_bonus
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
        env = make_env(self.deck, self.stake, on_refusal="truncate", max_steps=self.max_steps,
                       end_on_win=not self.endless_bonus)
        _, _, info = env.reset(seed=seed)
        self.seeds[seed] = {"result": None, "hash": {}, "open": 0, "waiting": []}
        return {"role": "main", "seed": seed, "env": env, "info": info, "steps": 0, "points": 0.0,
                "force": [], "veto": None,
                "blinds": 0, "shops": 0, "forced": set()}

    def _tick(self, model: Policy) -> None:
        while sum(line["role"] == "main" for line in self.lines) < self.mains:
            self.lines.append(self._new_main())
        views = [env_view(line["info"]) for line in self.lines]
        for k, line in enumerate(self.lines):
            if line["veto"] is not None:
                views[k] = shop_branches.without(views[k], line["info"]["game"], line["veto"])
        prep = Prepared(dataset.examples_of(views))
        greedy, glogp, _ = act(model, prep)
        tried = [k for k, line in enumerate(self.lines)
                 if line["role"] == "main" and self.rng.random() < self.rate]
        drawn, first = self._draw(model, prep, tried, greedy)
        for k in tried:
            self._states.append((views[k], *first[k]))
        drawn.update(self._forced(model, prep, greedy))
        sealed = self._sealed(model, prep, greedy, set(drawn))
        drawn.update(sealed)
        shop = self._shop(model, prep, views, greedy, set(drawn))
        self._follow(model, prep, views)
        spawned, still = [], []
        for k, line in enumerate(self.lines):
            move = greedy[k]
            if line["force"]:
                move = line["force"].pop(0)
                line["led"] = True
            if k in shop:
                alt, alt_logp, rest, veto, kind = shop[k]
                spawned.append(self._branch(line, views[k], greedy[k], float(glogp[k]), alt,
                                            alt_logp, rest=rest, veto=veto, shop=kind))
            elif k in drawn:
                alt, alt_logp = drawn[k]
                spawned.append(self._branch(line, views[k], greedy[k], float(glogp[k]), alt,
                                            alt_logp, seal=k in sealed))
            if self._step(line, move):
                still.append(line)
        self.lines = [line for line in still if not self._joined(line)] + spawned

    def _draw(self, model: Policy, prep: Prepared, tried: list[int], greedy) -> tuple[dict, dict]:
        """Per tried slot, a drawn move other than the most likely one, if one came up; and
        the first move drawn there, whatever it was."""
        found: dict[int, tuple[FactoredAction, float]] = {}
        first: dict[int, tuple[FactoredAction, float]] = {}
        left = list(tried)
        for _ in range(self.draws):
            if not left:
                break
            moves, logp, _ = act(model, prep, np.asarray(left), generator=self.generator)
            again = []
            for k, move, lp in zip(left, moves, logp):
                first.setdefault(k, (move, float(lp)))
                if same_move(move, greedy[k]):
                    again.append(k)
                else:
                    found[k] = (move, float(lp))
            left = again
        return found, first

    def _forced(self, model: Policy, prep: Prepared, greedy) -> dict:
        """Per main line where a ``FORCED`` move is legal for the first time in its
        occasion, is not the line's own pick and comes up at its chance: that move with
        the policy's likeliest item, and its log-probability. One per line per tick."""
        rows, types = [], []
        for k, line in enumerate(self.lines):
            if line["role"] != "main":
                continue
            legal = line["info"]["action_mask"].type_mask
            pick = None
            for ty, (when, chance) in FORCED.items():
                if not legal[ty]:
                    continue
                occasion = (ty, int(line["info"]["game"].ante) if when == "ante" else line[f"{when}s"])
                if occasion in line["forced"]:
                    continue
                line["forced"].add(occasion)
                if pick is None and greedy[k].action_type != ty and self.rng.random() < chance:
                    pick = ty
            if pick is not None:
                rows.append(k)
                types.append(pick)
        if not rows:
            return {}
        idx, types = np.asarray(rows), np.asarray(types)
        with torch.no_grad():
            t = prep.tokens(idx)
            run, tokens = model.encode(t)
            logp = torch.log_softmax(model.type_logits(run, t["type_mask"]), -1)
            logp = logp[torch.arange(len(idx)), torch.from_numpy(types)]
            moves, logp = _complete(model, prep, idx, run, tokens, types, logp, None)
        return {k: (move, float(lp)) for k, move, lp in zip(rows, moves, logp)}

    def _sealed(self, model: Policy, prep: Prepared, greedy, taken: set[int]) -> dict:
        """Per main line not in ``taken`` holding sealed cards (in hand at a play or
        discard, or in an opened pack): the move the policy makes with the seals hidden,
        or in a pack the seal Spectral it passes over, at the first decision point of the
        occasion where that is not the line's own pick and at its ``SEALED`` chance. With
        the move's log-probability under the policy, seals shown."""
        blind_rows, spectral = [], {}
        for k, line in enumerate(self.lines):
            if line["role"] != "main" or k in taken:
                continue
            g = line["info"]["game"]
            phase = g.phase.value
            if phase == "selecting_hand" and greedy[k].action_type in (PLAY, DISCARD):
                if any(c.seal for c in g.hand):
                    blind_rows.append(k)
            elif phase == "pack_opening":
                legal = line["info"]["action_mask"].entity_masks.get(ActionType.PickPackCard)
                passed = [i for i, c in enumerate(g.pack_cards) if c.key in SEAL_SPECTRALS
                          and legal is not None and legal[i]
                          and not (greedy[k].action_type == PICK and greedy[k].entity_target == i)]
                if passed:
                    spectral[k] = FactoredAction(PICK, entity_target=passed[0])
                elif any(c.seal and not c.face_down for c in g.pack_cards):
                    blind_rows.append(k)
        alts = dict(spectral)
        if blind_rows:
            blind = act(model, prep.without_seals(), np.asarray(blind_rows))[0]
            alts.update({k: m for k, m in zip(blind_rows, blind) if not same_move(m, greedy[k])})
        rows = []
        for k in alts:
            line = self.lines[k]
            kind = "pack" if line["info"]["game"].phase.value == "pack_opening" else "hand"
            occasion = ("seal " + kind, line["blinds"], line["shops"])
            if occasion in line["forced"]:
                continue
            line["forced"].add(occasion)
            if self.rng.random() < SEALED[kind]:
                rows.append(k)
        if not rows:
            return {}
        moves, logp = complete(model, prep, np.asarray(rows), [alts[k] for k in rows])
        return {k: (move, float(lp)) for k, move, lp in zip(rows, moves, logp)}

    def _follow(self, model: Policy, prep: Prepared, views) -> None:
        """Record each branch's forced move after its first, at the state it is made in,
        with its log-probability under the policy there."""
        rows = [k for k, line in enumerate(self.lines)
                if line["role"] == "branch" and line["force"] and line["led"]]
        if not rows:
            return
        moves, logp = complete(model, prep, np.asarray(rows), [self.lines[k]["force"][0] for k in rows])
        for k, move, lp in zip(rows, moves, logp):
            self.lines[k]["decision"]["follow"].append((views[k], move, float(lp)))

    def _shop(self, model: Policy, prep: Prepared, views, greedy, taken: set[int]) -> dict:
        """Per main line not in ``taken`` where a shop branch comes up (``rl.shop_branches``,
        in ``KINDS`` order, one per line per tick): the branch's first move with its
        log-probability under the policy, the moves after it, the card it may not buy and
        the branch's kind."""
        found: dict[int, object] = {}
        for k, line in enumerate(self.lines):
            if line["role"] != "main":
                continue
            shop_branches.note_held(line["info"]["game"], line)
            if k in taken:
                continue
            for kind in shop_branches.KINDS:
                occasion = shop_branches.occasion(kind, line)
                if occasion in line["forced"]:
                    continue

                def gate(occasion=occasion, kind=kind, line=line) -> bool:
                    line["forced"].add(occasion)
                    return self.rng.random() < SHOP_CHANCE[kind]

                p = shop_branches.propose(kind, line["info"], greedy[k], self.rng, line, gate)
                if p is not None:
                    found[k] = p
                    break
        if not found:
            return {}
        rows = list(found)
        firsts = []
        for k in rows:
            p = found[k]
            if p.veto is not None:
                masked = shop_branches.without(views[k], self.lines[k]["info"]["game"], p.veto)
                firsts.append(act(model, Prepared(dataset.examples_of([masked])))[0][0])
            else:
                firsts.append(p.moves[0])
        moves, logp = complete(model, prep, np.asarray(rows), firsts)
        out = {}
        for k, move, lp in zip(rows, moves, logp):
            if not same_move(move, greedy[k]):
                p = found[k]
                out[k] = (move, float(lp), list(p.moves[1:]), p.veto, p.kind)
        return out

    def _branch(self, main: dict, view, move, move_logp: float, alt, alt_logp: float,
                seal: bool = False, rest=(), veto: int | None = None,
                shop: str | None = None) -> dict:
        g = main["info"]["game"]
        env, info = copy_env(main["env"])
        self.seeds[main["seed"]]["open"] += 1
        decision = {"phase": g.phase.value, "ante": int(g.ante), "round": int(g.round),
                    "main move": ActionType(move.action_type).name,
                    "drawn move": ActionType(alt.action_type).name, "seal": seal,
                    "shop": shop, "pair": (view, move, move_logp, alt, alt_logp), "follow": []}
        return {"role": "branch", "seed": main["seed"], "env": env, "info": info, "steps": 0,
                "points": main["points"], "force": [alt, *rest], "led": False, "veto": veto,
                "decision": decision, "hashes": {}}

    def _step(self, line: dict, move: FactoredAction) -> bool:
        """Play one move; True while the line goes on."""
        game = line["info"]["game"]
        if line["role"] == "main":
            line["blinds"] += move.action_type in (ActionType.SelectBlind, ActionType.SkipBlind)
            line["shops"] += move.action_type == ActionType.CashOut
        if move.action_type == ActionType.PlayHand:
            move = replace(move, card_target=put_in_order(game, move.card_target))
        _, terminated, truncated, _, info = line["env"].step(move)
        if line["veto"] is not None and info["game"].phase.value != "shop":
            line["veto"] = None
        line["points"] += points(Transition.from_step(move, terminated, truncated, info),
                                 self.win_bonus, self.endless_bonus)
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
        result = {**result, "points": line["points"]}
        if line["role"] == "main":
            result = {**result, "seed": line["seed"], "steps": line["steps"]}
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
        follow = decision.pop("follow", [])
        if main is None:
            decision.update({"joined": True, "tied": True, "cut": False, "diff": 0.0,
                             "ante diff": 0, "win diff": 0})
            self._resolved.append(decision)
            return
        cut = main["cut"] or branch["cut"]
        diff = 0.0 if cut else main["points"] - branch["points"]
        decision.update({"joined": False, "tied": not cut and diff == 0.0, "cut": bool(cut),
                         "diff": diff, "ante diff": main["ante"] - branch["ante"],
                         "win diff": int(main["won"]) - int(branch["won"])})
        self._resolved.append(decision)
        if not cut and diff != 0.0:
            self._pairs.append((view, move, move_logp, alt, alt_logp, diff, follow))


def _result(info: dict, truncated: bool, stuck: bool) -> dict:
    """A line's end. A won game cut short in endless mode keeps its win and the points it
    earned there."""
    state = info["state"]
    won = bool(state.won)
    cut = None
    if stuck and not won:
        cut = "no legal action"
    elif truncated and not won:
        cut = next((f"{k}: {info[k]}" for k in ("refused", "not_ported", "vetoed") if k in info),
                   "step limit")
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
    for view, move, move_logp, alt, alt_logp, diff, *rest in pairs:
        follow = rest[0] if rest else []
        rows += [(view, move), (view, alt)] + [(v, m) for v, m, _ in follow]
        logp += [move_logp, alt_logp] + [lp for _, _, lp in follow]
        adv += [diff / 2, -diff / 2] + [-diff / 2] * len(follow)
    return (_examples(rows, deck, stake), np.asarray(logp, np.float32),
            np.asarray(adv, np.float32))


def tally(decisions: list[dict]) -> dict:
    """The resolved decisions summed: how many, how many tied, rejoined or cut, and the
    main line's moves against the drawn ones in points, antes and wins; over all of them,
    by phase, by the pair of move types ("PlayHand > Discard"), over the seal branches
    ("seal") and by shop branch ("shop <kind>")."""
    out: dict = {}
    for d in decisions:
        for key in ("all", d["phase"], f'{d["main move"]} > {d["drawn move"]}',
                    *(("seal",) if d.get("seal") else ()),
                    *((f"shop {d['shop']}",) if d.get("shop") else ())):
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
