"""The policy's input layout, its heads and its play, on the fixture games."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import torch
from jackdaw.engine.data import load
from jackdaw.env.action_space import ActionType

from rl import dataset
from rl.evaluate import DECK, STAKE, play, playstyle, summary
from rl.model import Policy, Prepared, Subsets, choose
from rl.train import heads, load_model, loss_of, measure, report, train

KEYS = [""] + sorted(load("centers"))


@pytest.fixture(scope="module")
def prep(shard) -> Prepared:
    return Prepared(shard)


@pytest.fixture(scope="module")
def model() -> Policy:
    torch.manual_seed(0)
    return Policy(width=32, layers=1).eval()


def test_subsets_are_every_selection_of_up_to_five_cards():
    s = Subsets.of(8)
    assert len(s.bits) == 218 == len(set(s.bits.tolist()))
    assert s.member.sum(1).tolist() == s.size.tolist()
    forced = s.legal(np.array([1]), np.array([5]), np.array([0b11111111]), np.array([3]))
    assert forced.sum() == 1 + 7 + 21 + 35 + 35  # every selection holds card 3
    some = s.legal(np.array([2]), np.array([2]), np.array([0b00001111]), np.array([-1]))
    assert some.sum() == 6


def test_tokens_follow_the_observation_lists(prep, shard):
    counts = np.stack([shard[n + "_n"] for n in ("hand", "joker", "cons", "shop", "pack")], 1)
    assert (prep.tok_len == counts.sum(1)).all()
    i = int(np.flatnonzero(counts.min(1) > 0)[0]) if (counts.min(1) > 0).any() else int(
        np.argmax(counts.sum(1)))
    lists = prep.cats[prep.tok_start[i]:prep.tok_start[i + 1], 0]
    assert lists.tolist() == [k + 1 for k in range(5) for _ in range(counts[i, k])]


def test_buy_labels_point_at_the_right_token(prep, shard):
    seen = set()
    for i in range(len(shard)):
        t = int(prep.act_type[i])
        prefix = {int(ActionType.RedeemVoucher): "v_", int(ActionType.OpenBooster): "p_",
                  int(ActionType.SellJoker): "j_", int(ActionType.UseConsumable): "c_"}.get(t)
        if prefix is None:
            continue
        one = np.array([i])
        base = prep.entity_base(one, prep.act_type[one])[0]
        token = prep.tok_start[i] + base + prep.act_entity[i]
        assert KEYS[prep.cats[token, 1]].startswith(prefix)
        mask = prep.entity_mask(one, prep.act_type[one], int(prep.tok_len[i]))[0]
        assert mask[base + prep.act_entity[i]]
        assert mask.sum() == shard.entity_mask(i, t).sum()
        seen.add(prefix)
    assert seen == {"v_", "p_", "j_", "c_"}


def test_every_stored_action_is_among_the_choices_the_heads_score(prep, model):
    with torch.no_grad():
        out = heads(model, prep, np.arange(prep.n))  # raises if a stored selection is illegal
    logits, label = out["type"]
    assert torch.isfinite(logits[torch.arange(len(label)), label]).all()
    _, logits, label, _ = out["item"]
    assert torch.isfinite(logits[torch.arange(len(label)), label]).all()
    assert sum(len(g) for g, *_ in out["sets"]) == int((prep.act_cards != 0).sum())
    assert torch.isfinite(loss_of(out))


def test_chosen_actions_are_legal(prep, model, shard):
    actions = choose(model, prep)
    assert len(actions) == len(shard)
    for i, action in enumerate(actions):
        t = action.action_type
        assert shard["type_mask"][i][t]
        if action.entity_target is not None:
            assert shard.entity_mask(i, t)[action.entity_target]
        if t in (ActionType.PlayHand, ActionType.Discard):
            cards = action.card_target
            assert 1 <= len(cards) <= 5
            assert shard.rows("card_mask", i)[list(cards)].all()
            forced = int(shard["forced"][i])
            assert forced < 0 or forced in cards


def test_training_lowers_the_loss_and_agreement_is_reported(prep, shard):
    idx = np.arange(prep.n)
    model, prior, history = train(
        prep, idx, idx, steps=60, batch=128, lr=2e-3, config={"width": 32, "layers": 1},
        log=lambda line: None,
    )
    with torch.no_grad():
        fresh = float(loss_of(heads(Policy(width=32, layers=1), prep, idx[:512])))
        trained = float(loss_of(heads(model.eval(), prep, idx[:512])))
    assert trained < fresh
    r = report(measure(model, prep, idx, prior))
    assert r["held-out examples"] == prep.n
    assert 0 <= r["cards"] <= 1 and 0 <= r["item"] <= 1
    assert r["action type"] > r["action type, most common legal type"] - 0.2
    assert set(r["by action type"]) >= {"PlayHand", "BuyCard", "SwapJokersLeft"}


def test_a_policy_plays_games_to_an_end_without_a_refused_action(model, shard):
    games = [(str(shard["g_seed"][g]), str(shard["g_deck"][g]), int(shard["g_stake"][g]))
             for g in range(shard.n_games)]
    rows = play(model, games, parallel=3, max_steps=400)
    assert sorted(r["seed"] for r in rows) == sorted(g[0] for g in games)
    for r in rows:
        assert r["cut"] in (None, "step limit"), r
        assert 1 <= r["ante"] <= 9
    s = summary(rows)
    assert s["games"] == 5 and 0 <= s["win rate"] <= 1
    style = playstyle(rows)
    assert sum(h["per game"] for h in style["hands"].values()) > 0
    assert all(h["level"] >= 1 for h in style["hands"].values())
    assert all(k.startswith("j_") and 0 < v <= 1 for k, v in style["jokers"].items())


def test_recorded_games_list_every_hand_played_and_each_pack_pick(model, shard):
    games = [(str(shard["g_seed"][g]), str(shard["g_deck"][g]), int(shard["g_stake"][g]))
             for g in range(shard.n_games)]
    rows = play(model, games, parallel=3, max_steps=400, record=True)
    for r in rows:
        counted = {}
        for _, hand in r["plays"]:
            counted[hand] = counted.get(hand, 0) + 1
        assert counted == {n: p for n, (p, _) in r["hands"].items() if p}
        assert [s for s, _ in r["plays"]] == sorted(s for s, _ in r["plays"])
        for pick in r["packs"]:
            assert pick["offer"] and (pick["pick"] is None or pick["pick"] in pick["offer"])
    assert "packs" not in play(model, games[:1], parallel=1, max_steps=50)[0]


WINNER = Path(__file__).resolve().parents[1] / "data" / "ppo" / "decisions6k"


@pytest.mark.skipif(not (WINNER / "u00018.pt").exists(), reason="needs a model that wins")
def test_endless_play_keeps_the_won_game_and_adds_how_far_it_went():
    model, _ = load_model(WINNER / "u00018.pt")
    checked = json.loads((WINNER / "check_u00018.json").read_text())["games"]
    games = [(g["seed"], DECK, STAKE) for g in checked if g["won"]][:2]
    games.append(next((g["seed"], DECK, STAKE) for g in checked if not g["won"]))
    plain = {r["seed"]: r for r in play(model, games, parallel=3)}
    endless = {r["seed"]: r for r in play(model, games, parallel=3, endless=True)}
    assert summary(list(endless.values()))["endless ante"] >= 9
    for seed, r in endless.items():
        base = plain[seed]
        if base["won"]:
            assert r.pop("endless ante") >= 9 and "endless cut" in r
            r.pop("endless cut")
        assert r == base


def test_playstyle_averages_over_every_game():
    rows = [{"hands": {"Pair": [4, 2], "Flush": [1, 1]}, "jokers": ["j_mad", "j_mad", "j_egg"]},
            {"hands": {"Pair": [2, 1]}, "jokers": ["j_mad"]}]
    style = playstyle(rows)
    assert style["hands"]["Pair"] == {"per game": 3.0, "level": 1.5}
    assert style["hands"]["Flush"] == {"per game": 0.5, "level": 1.0}
    assert style["jokers"] == {"j_mad": 1.0, "j_egg": 0.5}


def test_views_become_examples_for_the_model(shard):
    from rl.playback import env_view, make_env

    env = make_env("b_blue", 1)
    _, _, info = env.reset(seed=str(shard["g_seed"][0]))
    ex = dataset.examples_of([env_view(info)])
    assert len(ex) == 1
    assert np.array_equal(ex["glob"][0], shard["glob"][shard.game_examples(0)[0]])
