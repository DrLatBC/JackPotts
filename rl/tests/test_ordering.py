"""Automatic play order: the cards a policy chose, played in the bot's order."""

from __future__ import annotations

from jackdaw.engine import actions as A
from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction

import random
from itertools import combinations

from rl import dataset
from rl.ordering import bot_order, bot_state, order_can_change, put_in_order
from rl.playback import apply_pre, make_env


def _what_the_bot_did(shard, i, hand_size) -> list[int]:
    """The order the bot played example ``i``'s cards in, by their places in the hand
    before its reorder."""
    pre = [int(c) for c in shard.rows("pre", i)]
    if not pre:
        return sorted(shard.action(i).card_target)
    perm = list(range(hand_size))
    for code in pre:
        at = code - dataset.PRE_HAND_LEFT
        perm[at - 1], perm[at] = perm[at], perm[at - 1]
    return [perm[j] for j in sorted(shard.exec_action(i).card_target)]


def _plays(shard, g):
    """Game ``g`` played back; yields ``(example, game)`` before each stored play."""
    env = make_env(str(shard["g_deck"][g]), int(shard["g_stake"][g]))
    _, _, info = env.reset(seed=str(shard["g_seed"][g]))
    for i in shard.game_examples(g):
        if int(shard["act_type"][i]) == ActionType.PlayHand:
            yield i, info["game"]
        apply_pre(info["game"], shard.rows("pre", i))
        _, _, _, _, info = env.step(shard.exec_action(i))


def test_the_bot_sees_its_usual_state(shard):
    env = make_env("b_blue", 1)
    _, _, info = env.reset(seed=str(shard["g_seed"][0]))
    state = bot_state(info["game"])
    assert state["state"] == "BLIND_SELECT"
    assert "counters" in state  # a field only the fork's view carries


def test_the_rule_gives_the_order_the_bot_played_in(shard):
    plays = reordered = same = 0
    for g in range(shard.n_games):
        for i, game in _plays(shard, g):
            if int(shard["flags"][i]) & dataset.BELL:
                continue  # the recorded bot did not choose the forced card
            did = _what_the_bot_did(shard, i, len(game.hand))
            plays += 1
            reordered += did != sorted(did)
            same += bot_order(game, shard.action(i).card_target) == did
    assert plays > 100 and reordered >= 10
    assert same == plays


def _order_holds(game, cards) -> bool:
    """The quick check never says "hand order" where the full rule gives another."""
    return order_can_change(game, cards) or bot_order(game, cards) == sorted(cards)


def test_the_quick_check_agrees_with_the_full_rule(shard):
    rng = random.Random(0)
    checked = skipped = 0
    for g in range(shard.n_games):
        for i, game in _plays(shard, g):
            hand = range(len(game.hand))
            picks = [shard.action(i).card_target] + [
                rng.choice(list(combinations(hand, k))) for k in (2, 3, 4, 5) if k <= len(game.hand)]
            for cards in picks:
                assert _order_holds(game, cards), (g, i, cards)
                checked += 1
                skipped += not order_can_change(game, cards)
    assert checked > 400 and skipped > 0


def test_the_quick_check_sees_glass_and_the_order_jokers(shard):
    env = make_env("b_blue", 1)
    _, _, info = env.reset(seed=str(shard["g_seed"][0]))
    game = info["game"]
    game.step(A.SelectBlind())
    plain = (0, 1)
    assert not order_can_change(game, plain)
    game.debug_add("H_A", enhancement="m_glass")
    glass = next(k for k, c in enumerate(game.hand) if c.key == "m_glass")
    other = next(k for k in range(len(game.hand)) if k != glass)
    assert order_can_change(game, (other, glass))
    assert bot_order(game, sorted([glass, other]))[-1] == glass
    plain = tuple(k for k in range(len(game.hand)) if k != glass)[:2]
    assert not order_can_change(game, plain)
    game.debug_add("j_triboulet")
    assert order_can_change(game, (0, 1))
    assert not order_can_change(game, (0,))


def test_games_end_as_recorded_when_the_wrapper_orders_the_plays(shard):
    """Feed back only which cards the bot played; the wrapper puts them in order."""
    for g in (2, 3, 4):  # the three losses
        env = make_env(str(shard["g_deck"][g]), int(shard["g_stake"][g]))
        _, _, info = env.reset(seed=str(shard["g_seed"][g]))
        for i in shard.game_examples(g):
            action = shard.action(i)
            if action.action_type == ActionType.PlayHand:
                cards = put_in_order(info["game"], action.card_target)
                action = FactoredAction(int(ActionType.PlayHand), card_target=cards)
            else:
                apply_pre(info["game"], shard.rows("pre", i))
            _, terminated, truncated, _, info = env.step(action)
        state = info["state"]
        assert terminated and not truncated
        assert (state.ante, state.round, state.dollars) == tuple(int(v) for v in shard["g_final"][g])


def test_cards_already_in_order_are_left_where_they_are(shard):
    i, game = next(
        (i, game) for i, game in _plays(shard, 3) if not len(shard.rows("pre", i))
        and not int(shard["flags"][i]) & dataset.BELL)
    before = [c.sort_id for c in game.hand]
    cards = shard.action(i).card_target
    assert put_in_order(game, cards) == cards
    assert [c.sort_id for c in game.hand] == before


def test_a_reordered_play_puts_the_cards_at_the_front(shard):
    found = None
    for g in range(shard.n_games):
        for i, game in _plays(shard, g):
            if len(shard.rows("pre", i)) and not int(shard["flags"][i]) & dataset.BELL:
                found = (i, game)
                break
        if found:
            break
    i, game = found
    did = _what_the_bot_did(shard, i, len(game.hand))
    ids = [game.hand[k].sort_id for k in did]
    cards = put_in_order(game, shard.action(i).card_target)
    assert cards == tuple(range(len(did)))
    assert [c.sort_id for c in game.hand][:len(did)] == ids
