"""The boss-rule mask: plays The Eye or The Mouth would score 0 are never offered to the
model while another play still scores."""

from __future__ import annotations

import numpy as np
import torch
from jackdaw.core import Game
from jackdaw.engine import actions as A
from jackdaw.env.action_space import ActionType

from rl import dataset
from rl.dataset import bits_to_cards
from rl.features import view_of
from rl.model import Policy, Prepared, act, choose, set_groups


def boss_game(boss: str) -> Game:
    """A new run whose small blind is ``boss``, with that blind selected."""
    snap = Game.new("b_blue", 1, "BOSS-MASK").to_dict()
    snap["round_resets"]["blind_choices"]["Small"] = boss
    game = Game.from_dict(snap)
    game.step(A.SelectBlind())
    return game


def hand_type(game: Game, bits: int) -> str:
    return game.preview_play(list(bits_to_cards(bits))).hand_type


def legal_plays(view) -> list[int]:
    """The card selections the model may play from this view, as bits."""
    prep = Prepared(dataset.examples_of([view]))
    idx = np.zeros(1, np.int64)
    types = np.array([int(ActionType.PlayHand)])
    (_, subsets, legal), = set_groups(prep, idx, np.zeros(1, np.int64), types, np.full(1, -1))
    return [int(b) for b in subsets.bits[legal[0]]]


def test_nothing_is_masked_before_the_first_hand():
    assert view_of(boss_game("bl_eye")).zeroed == ()
    assert view_of(boss_game("bl_mouth")).zeroed == ()


def test_the_eye_masks_every_play_of_a_type_already_played():
    game = boss_game("bl_eye")
    game.step(A.PlayHand(card_indices=(0,)))
    view = view_of(game)
    assert view.zeroed and all(hand_type(game, b) == "High Card" for b in view.zeroed)
    plays = legal_plays(view)
    assert plays and all(hand_type(game, b) != "High Card" for b in plays)


def test_the_mouth_masks_every_play_but_the_first_type():
    game = boss_game("bl_mouth")
    game.step(A.PlayHand(card_indices=(0,)))
    view = view_of(game)
    plays = legal_plays(view)
    assert plays and all(hand_type(game, b) == "High Card" for b in plays)
    assert view.zeroed and all(hand_type(game, b) != "High Card" for b in view.zeroed)


def test_when_every_play_scores_zero_none_is_masked():
    game = boss_game("bl_mouth")
    pair = next((i, j) for i in range(len(game.hand)) for j in range(i)
                if game.hand[i].rank == game.hand[j].rank)
    game.step(A.PlayHand(card_indices=pair))
    ranks, ids = set(), []
    for card in (*game.hand, *game.deck):
        if card.rank not in ranks and len(ids) < len(game.hand):
            ranks.add(card.rank)
            ids.append(card.sort_id)
    game.debug_set_hand(ids)
    view = view_of(game)
    assert view.zeroed == ()
    assert len(legal_plays(view)) == len(view.legal_card_sets(int(ActionType.PlayHand), None))


def test_a_model_never_plays_a_masked_hand():
    game = boss_game("bl_eye")
    game.step(A.PlayHand(card_indices=(0,)))
    view = view_of(game)
    torch.manual_seed(0)
    model = Policy(width=16, layers=1)
    prep = Prepared(dataset.examples_of([view] * 64))
    drawn, _, _ = act(model, prep, generator=torch.Generator().manual_seed(0))
    plays = [a for a in [*drawn, *choose(model, prep)] if a.action_type == ActionType.PlayHand]
    assert plays
    for action in plays:
        assert hand_type(game, sum(1 << c for c in action.card_target)) != "High Card"


def test_shards_from_before_the_mask_zero_nothing(shard):
    arrays = {k: v for k, v in shard.arrays.items() if not k.startswith("zeroed")}
    ex = dataset.Examples(arrays)
    assert len(ex["zeroed_n"]) == len(ex) and not ex["zeroed_n"].any()
    Prepared(ex)
