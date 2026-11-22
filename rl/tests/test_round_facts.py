"""The round columns (the boss in force, hand types played this round) and the widening
that lets networks saved without them load and act as they were saved."""

from __future__ import annotations

import numpy as np
import torch
from jackdaw.core import Game
from jackdaw.engine import actions as A
from jackdaw.env.observation import D_GLOBAL

from rl import dataset
from rl.features import BOSS_KEYS, D_ROUND, HAND_TYPES, round_facts
from rl.model import D_RUN, Policy, widen_inputs, widen_optimizer


def mouth_game() -> Game:
    """A new run whose small blind is The Mouth."""
    snap = Game.new("b_blue", 1, "ROUND-FACTS").to_dict()
    snap["round_resets"]["blind_choices"]["Small"] = "bl_mouth"
    return Game.from_dict(snap)


def column(name: str) -> int:
    if name in BOSS_KEYS:
        return BOSS_KEYS.index(name)
    return len(BOSS_KEYS) + HAND_TYPES.index(name)


def test_round_columns_follow_the_boss_and_the_hand_types_played():
    game = mouth_game()
    assert not round_facts(game).any()  # blind select: no blind in play

    game.step(A.SelectBlind())
    facts = round_facts(game)
    assert np.flatnonzero(facts).tolist() == [column("bl_mouth")]

    game.step(A.PlayHand(card_indices=(0,)))
    facts = round_facts(game)
    assert np.flatnonzero(facts).tolist() == [column("bl_mouth"), column("High Card")]

    game.debug_set(chips=100_000)
    game.step(A.PlayHand(card_indices=(0,)))
    game.step(A.CashOut())
    assert not round_facts(game).any()  # the shop keeps the counts, the columns do not


def test_a_network_saved_without_the_round_columns_ignores_them():
    torch.manual_seed(0)
    saved = Policy(width=16, layers=1).state_dict()
    saved["run_in.0.weight"] = saved["run_in.0.weight"][:, :D_GLOBAL].clone()
    model = Policy(width=16, layers=1)
    model.load_state_dict(widen_inputs(saved))
    glob = torch.rand(4, D_GLOBAL)
    plain = model.run_in(torch.cat([glob, torch.zeros(4, D_ROUND)], 1))
    with_round = model.run_in(torch.cat([glob, torch.ones(4, D_ROUND)], 1))
    assert torch.equal(plain, with_round)
    narrow = torch.nn.functional.linear(glob, saved["run_in.0.weight"], saved["run_in.0.bias"])
    assert torch.allclose(model.run_in[0](torch.cat([glob, torch.ones(4, D_ROUND)], 1)), narrow)


def test_optimizer_moments_for_the_narrow_input_are_padded():
    model = Policy(width=16, layers=1)
    opt = torch.optim.Adam(model.parameters())
    weight = model.run_in[0].weight
    opt.state[weight] = {"step": torch.tensor(3.0), "exp_avg": torch.ones(16, D_GLOBAL),
                         "exp_avg_sq": torch.ones(16, D_GLOBAL)}
    widen_optimizer(opt)
    assert opt.state[weight]["exp_avg"].shape == (16, D_RUN)
    assert not opt.state[weight]["exp_avg"][:, D_GLOBAL:].any()


def test_examples_without_round_columns_read_as_zeros(shard):
    arrays = {k: v for k, v in shard.arrays.items() if k != "round"}
    ex = dataset.Examples(arrays)
    assert ex["round"].shape == (len(ex), D_ROUND)
    assert not ex["round"].any()
