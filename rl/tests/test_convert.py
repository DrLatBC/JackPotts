"""The converter's special cases and the no-model test, on a small fixed replay file.

    .venv-rl\\Scripts\\python.exe -m pytest rl/tests -q

The fixture games (``conftest.py``) contain every case below between them.
"""

from __future__ import annotations

import importlib.metadata
import random

import numpy as np
from jackdaw.engine.data import load
from jackdaw.env.action_space import ACTION_VERSION, ActionType

from balatro_bot.replays import ReplayWriter
from rl import dataset
from rl.convert import MAX_PLAY, left_moves, selection, usable_records
from rl.features import AURA_ID, item_ids
from rl.playback import play_back

KEYS = [""] + sorted(load("centers"))  # item id -> center key; 0 is unknown


def before_win(record: dict, stats) -> list:
    """The requests the examples cover: everything up to the win."""
    acts = record["actions"]
    return acts[1:len(acts) - stats["dropped_after_win"]]


def all_examples(converted):
    return [e for _, examples, _ in converted for e in examples]


def of_type(converted, action_type):
    return [e for e in all_examples(converted) if e.action.action_type == action_type]


# -- the pieces --------------------------------------------------------------------


def test_left_moves_reach_the_order():
    rng = random.Random(7)
    for n in range(1, 9):
        for _ in range(50):
            perm = list(range(n))
            rng.shuffle(perm)
            order = list(range(n))
            for at in left_moves(perm):
                order[at - 1], order[at] = order[at], order[at - 1]
            assert order == perm


def test_selection_takes_the_forced_card_first():
    assert selection([6, 7, 5], None, MAX_PLAY) == (5, 6, 7)
    assert selection([3, 4, 6], 5, MAX_PLAY) == (3, 4, 5, 6)
    # Five cards without the forced one: the request's last card is left out.
    assert selection([1, 3, 4, 6, 7], 0, MAX_PLAY) == (0, 1, 3, 4, 6)
    assert selection([0, 1, 3, 5, 6], 5, MAX_PLAY) == (0, 1, 3, 5, 6)


def test_exploring_games_and_repeated_seeds_are_left_out(records, tmp_path):
    path = tmp_path / "replay_1.jsonl.gz"
    writer = ReplayWriter(path)
    writer.write(records[2])
    writer.write({**records[3], "explore_rate": 0.1})
    writer.write(records[2])
    writer.close()
    assert [r["seed"] for _, r in usable_records([path])] == [records[2]["seed"]]


# -- conversion --------------------------------------------------------------------


def test_every_game_converts(converted, records):
    assert len(converted) == len(records) == 5
    for record, examples, stats in converted:
        assert examples
        assert stats["won"] == record["won"]


def test_nothing_after_a_win(converted):
    wins = [(r, ex, s) for r, ex, s in converted if r["won"]]
    assert len(wins) == 2
    for record, examples, stats in wins:
        assert stats["dropped_after_win"] > 0
        assert record["final"]["ante"] >= 9
    for record, _, stats in converted:
        if not record["won"]:
            assert stats["dropped_after_win"] == 0


def test_hand_reorder_folds_into_one_play(converted):
    seen = bell = 0
    for record, examples, stats in converted:
        acts = before_win(record, stats)
        pairs = [
            (acts[i][1]["hand"], len(acts[i + 1][1]["cards"]))
            for i in range(len(acts) - 1)
            if acts[i][0] == "rearrange" and "hand" in acts[i][1] and acts[i + 1][0] == "play"
        ]
        folded = [e for e in examples if e.flags & dataset.FOLDED]
        assert len(folded) == len(pairs) == stats["folded"]
        for e, (perm, k) in zip(folded, pairs):
            assert e.action.action_type == ActionType.PlayHand
            assert e.pre == [dataset.PRE_HAND_LEFT + at for at in left_moves(perm)]
            # The cards the model is asked for are the ones the bot chose, by their
            # places before the reorder; the replay plays them from the front.
            assert e.action.card_target == tuple(sorted(perm[j] for j in e.exec_cards))
            if e.flags & dataset.BELL:
                bell += 1
                assert e.view.mask.forced_card in e.action.card_target
            else:
                assert e.exec_cards == tuple(range(k))
                assert e.action.card_target == tuple(sorted(perm[:k]))
            seen += 1
    assert seen >= 10 and bell > 0
    assert any(e.flags & dataset.ORDER_MATTERS for e in all_examples(converted))


def test_no_hand_swaps_come_from_a_folded_play(converted):
    for record, examples, stats in converted:
        acts = before_win(record, stats)
        other = sum(
            len(left_moves(acts[i][1]["hand"]))
            for i in range(len(acts))
            if acts[i][0] == "rearrange" and "hand" in acts[i][1]
            and not (i + 1 < len(acts) and acts[i + 1][0] == "play")
        )
        swaps = [e for e in examples if e.action.action_type == ActionType.SwapHandLeft]
        assert len(swaps) == other == stats["hand_swap_examples"]
    assert of_type(converted, ActionType.SwapHandLeft)


def test_joker_reorder_becomes_single_swaps(converted):
    for record, examples, stats in converted:
        moves = [
            at
            for method, p in before_win(record, stats)
            if method == "rearrange" and "jokers" in p
            for at in left_moves(p["jokers"])
        ]
        swaps = [e for e in examples if e.action.action_type == ActionType.SwapJokersLeft]
        assert [e.action.entity_target for e in swaps] == moves
        assert all(e.pre == [] and e.action.card_target is None for e in swaps)
    assert len(of_type(converted, ActionType.SwapJokersLeft)) > 10


def test_buys_point_at_their_own_part_of_the_shop(converted):
    def key(e, offset):
        ids = item_ids(e.view.obs.shop_cards[:, 0])
        return KEYS[ids[offset + e.action.entity_target]]

    vouchers = of_type(converted, ActionType.RedeemVoucher)
    boosters = of_type(converted, ActionType.OpenBooster)
    cards = of_type(converted, ActionType.BuyCard)
    assert vouchers and boosters and cards
    for e in vouchers:
        assert key(e, e.view.splits[0]).startswith("v_")
    for e in boosters:
        assert key(e, e.view.splits[0] + e.view.splits[1]).startswith("p_")
    for e in cards:
        assert e.action.entity_target < e.view.splits[0]
        assert not key(e, 0).startswith(("v_", "p_"))
    # The three parts add up to the shop's rows, and some shop has more than one part.
    assert all(sum(e.view.splits) == len(e.view.obs.shop_cards) for e in all_examples(converted))
    assert any(e.view.splits[0] and e.view.splits[1] for e in vouchers)


def test_targeted_consumable_names_its_cards(converted):
    uses = [e for e in of_type(converted, ActionType.UseConsumable) if e.action.card_target]
    assert uses
    for e in uses:
        low, high = e.view.card_bounds(e.action.action_type, e.action.entity_target)
        assert low <= len(e.action.card_target) <= high
        assert e.action.card_target in e.view.legal_card_sets(
            e.action.action_type, e.action.entity_target
        )
    plain = [e for e in of_type(converted, ActionType.UseConsumable) if not e.action.card_target]
    assert plain
    for e in plain:
        assert e.view.card_bounds(e.action.action_type, e.action.entity_target) is None


def test_pack_cards_carry_what_the_observation_leaves_out(converted):
    picks = of_type(converted, ActionType.PickPackCard)
    assert picks
    named = 0
    for e in picks:
        rows, extra = e.view.obs.pack_cards, e.view.pack_extra
        assert len(rows) == len(extra)
        assert all(KEYS[int(i)] for i in extra[:, 0])
        named += KEYS[int(extra[e.action.entity_target, 0])].startswith(("j_", "c_"))
    assert named
    targeted = [e for e in picks if e.action.card_target]
    assert targeted
    for e in targeted:
        assert e.action.card_target in e.view.legal_card_sets(
            e.action.action_type, e.action.entity_target
        )


def test_aura_takes_exactly_one_card_without_an_edition(converted):
    auras = [
        e for e in of_type(converted, ActionType.PickPackCard)
        if int(e.view.pack_extra[e.action.entity_target, 0]) == AURA_ID
    ]
    assert auras
    for e in auras:
        assert e.view.card_bounds(e.action.action_type, e.action.entity_target) == (1, 1)
        assert len(e.action.card_target) == 1


# -- the shard and the no-model test -----------------------------------------------


def test_shard_is_stamped_and_keeps_every_example(shard, converted):
    assert str(shard["jackdaw"]) == importlib.metadata.version("jackdaw")
    assert int(shard["action_version"]) == ACTION_VERSION
    examples = all_examples(converted)
    assert len(shard) == len(examples)
    assert shard.n_games == len(converted)
    for i in random.Random(3).sample(range(len(examples)), 200):
        e = examples[i]
        assert shard.action(i) == e.action
        assert np.array_equal(shard.rows("hand", i), e.view.obs.hand_cards)
        assert np.array_equal(shard.rows("pre", i), np.asarray(e.pre, np.int16))
        for t, mask in e.view.mask.entity_masks.items():
            assert np.array_equal(shard.entity_mask(i, t), mask)


def test_every_stored_action_is_legal_under_its_stored_mask(shard):
    for i in range(len(shard)):
        action = shard.action(i)
        assert shard["type_mask"][i][action.action_type]
        if action.entity_target is not None:
            assert shard.entity_mask(i, action.action_type)[action.entity_target]
        if action.card_target is not None:
            assert shard.rows("card_mask", i)[list(action.card_target)].all()
            forced = int(shard["forced"][i])
            assert forced < 0 or forced in action.card_target


def test_games_play_back_through_the_environment(shard):
    """The no-model test: the stored actions, fed to the environment as a model's
    would be, meet the stored observation and mask at every step and end each game
    where the bot's ended."""
    for g in range(shard.n_games):
        assert play_back(shard, g) is None, str(shard["g_seed"][g])


def test_playback_notices_a_changed_example(shard):
    def changed(name, i, value):
        arrays = dict(shard.arrays)
        arrays[name] = arrays[name].copy()
        arrays[name][i] = value
        return dataset.Examples(arrays)

    g = 3  # the ante 5 loss
    steps = shard.game_examples(g)
    plays = [i for i in steps if shard["act_type"][i] == ActionType.PlayHand
             and not shard["flags"][i] & dataset.FOLDED]
    i = plays[2]
    other = dataset.cards_to_bits([0]) if int(shard["exec_cards"][i]) != 1 else 2
    assert play_back(changed("exec_cards", i, other), g) is not None
    assert "global" in play_back(changed("glob", steps[5], 0.123), g)
    assert "type mask" in play_back(changed("type_mask", steps[5], True), g)
    wrong_end = changed("g_final", g, shard["g_final"][g] + 1)
    assert "recorded" in play_back(wrong_end, g)
