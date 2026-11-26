"""Shop branches: when each comes up, what it tries, and how a branch line plays it."""

from __future__ import annotations

import copy

import numpy as np
import pytest
import torch
from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction

from rl import branch, dataset, shop_branches as sb
from rl.branch import BranchPlay
from rl.model import Policy, Prepared
from rl.playback import copy_env, env_view
from rl.ppo import scored
from rl.tests.test_branch import at_first_shop

LEAVE = FactoredAction(int(ActionType.NextRound))


@pytest.fixture(scope="module")
def model() -> Policy:
    torch.manual_seed(0)
    return Policy(width=32, layers=1).eval()


@pytest.fixture
def shop():
    """A branch player and its one main line in run "test"'s first shop, which sells Egg
    ($4) and Mars ($3), with $11."""
    bp = BranchPlay("test", rate=0.0, mains=1)
    bp._pairs, bp._states, bp._finished, bp._resolved = [], [], [], []
    main = at_first_shop(bp)
    bp.lines = [main]
    return bp, main


def refreshed(main: dict) -> dict:
    """The line with its step info read again after debug edits to its game."""
    main["env"], main["info"] = copy_env(main["env"])
    return main


def index_of(game, key: str) -> int:
    return next(i for i, c in enumerate(game.shop_jokers) if c.key == key)


class Gate:
    def __init__(self, answer: bool = True) -> None:
        self.answer, self.asked = answer, 0

    def __call__(self) -> bool:
        self.asked += 1
        return self.answer


def test_a_joker_that_adds_mult_raises_the_hand_score(shop):
    _, main = shop
    game = main["info"]["game"]
    hands = sb.dealt_hands(game, main["info"]["state"], np.random.default_rng(0))
    base = sb.hand_score(game, hands)
    game.debug_add("j_joker")
    assert sb.hand_score(game, hands) > base > 0


def test_buy_plans_sell_what_a_buy_needs(shop):
    _, main = shop

    def plans():
        info = refreshed(main)["info"]
        return sb.buy_plans(info["game"], info["state"], info["action_mask"], egg)

    game = main["info"]["game"]
    egg = index_of(game, "j_egg")
    assert plans() == [[FactoredAction(sb.BUY, entity_target=egg)]]

    main["info"]["game"].debug_set(money=3)
    main["info"]["game"].debug_add("c_pluto")
    assert [int(m.action_type) for m in plans()[0]] == [sb.SELL_CONS, sb.BUY]

    main["info"]["game"].debug_set(money=0)
    assert plans() == []
    main["info"]["game"].debug_set(money=3)
    for key in ("j_joker", "j_jolly", "j_zany", "j_mad", "j_crazy"):
        main["info"]["game"].debug_add(key)
    got = plans()
    assert got and all(int(p[0].action_type) == sb.SELL_JOKER and int(p[-1].action_type) == sb.BUY
                       for p in got)


def test_a_copy_joker_on_sale_is_bought_once_its_gate_says_so(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "COPY_JOKERS", frozenset({"j_egg"}))
    game = main["info"]["game"]
    egg = index_of(game, "j_egg")
    closed = Gate(False)
    assert sb.propose("copy joker", main["info"], LEAVE, np.random.default_rng(0), main, closed) is None
    assert closed.asked == 1
    p = sb.propose("copy joker", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p.moves == [FactoredAction(sb.BUY, entity_target=egg)]
    buying = FactoredAction(sb.BUY, entity_target=egg)
    assert sb.propose("copy joker", main["info"], buying, np.random.default_rng(0), main, Gate()) is None


def test_midas_mask_is_wanted_only_beside_vampire(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "MIDAS", "j_egg")
    game = main["info"]["game"]
    assert sb.propose("midas", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None
    game.debug_add("j_vampire")
    refreshed(main)
    p = sb.propose("midas", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p.moves == [FactoredAction(sb.BUY, entity_target=index_of(game, "j_egg"))]


def test_a_planet_for_a_hand_the_run_does_not_play_is_vetoed(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "MIN_PLAYS", 1)
    game = main["info"]["game"]
    mars = index_of(game, "c_mars")
    buying = FactoredAction(sb.BUY, entity_target=mars)
    p = sb.propose("off-build planet", main["info"], buying, np.random.default_rng(0), main, Gate())
    assert p.moves == [] and p.veto == game.shop_jokers[mars].sort_id
    view = sb.without(env_view(main["info"]), game, p.veto)
    assert not view.mask.entity_masks[ActionType.BuyCard][mars]
    assert view.mask.entity_masks[ActionType.BuyCard][index_of(game, "j_egg")]
    monkeypatch.setattr(sb, "MIN_PLAYS", 10)
    assert sb.propose("off-build planet", main["info"], buying, np.random.default_rng(0), main, Gate()) is None


def test_best_joker_is_tried_more_often_late():
    assert sb.chance("best joker", sb.LATE_ANTE - 1) == sb.SHOP_CHANCE["best joker"] == 0.1
    assert sb.chance("best joker", sb.LATE_ANTE) == sb.chance("best joker", 8) == 0.5
    assert sb.chance("fill slot", 8) == sb.SHOP_CHANCE["fill slot"]


def test_partners_pair_either_way_round(monkeypatch):
    monkeypatch.setattr(sb, "PAIRS", [("j_pareidolia", frozenset({"j_smiley", "j_photograph"}))])
    assert sb.partners("j_pareidolia", {"j_smiley", "j_joker"}) == {"j_smiley"}
    assert sb.partners("j_smiley", {"j_pareidolia"}) == {"j_pareidolia"}
    assert sb.partners("j_smiley", {"j_photograph"}) == frozenset()


def test_bot_tables_name_real_jokers():
    from jackdaw.engine.data import load
    centers = load("centers")
    keys = {k for e, b in sb.PAIRS for k in (e, *b)}
    keys |= set(sb.CONFLICTS) | {k for v in sb.CONFLICTS.values() for k in v}
    assert keys <= set(centers)


def test_madness_conflicts_with_jokers_that_store_value():
    held = sb.CONFLICTS["j_madness"]
    assert {"j_hologram", "j_campfire", "j_blueprint", "j_perkeo", "j_egg"} <= held
    assert not {"j_fortune_teller", "j_throwback", "j_steel_joker", "j_ice_cream"} & held


@pytest.mark.parametrize("pairs", [
    [("j_egg", frozenset({"j_joker"}))],
    [("j_joker", frozenset({"j_egg"}))],
])
def test_a_joker_that_pairs_with_a_held_one_is_bought(shop, monkeypatch, pairs):
    _, main = shop
    monkeypatch.setattr(sb, "PAIRS", pairs)
    game = main["info"]["game"]
    assert sb.propose("synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None
    game.debug_add("j_joker")
    refreshed(main)
    egg = index_of(game, "j_egg")
    p = sb.propose("synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p.moves == [FactoredAction(sb.BUY, entity_target=egg)]
    buying = FactoredAction(sb.BUY, entity_target=egg)
    assert sb.propose("synergy", main["info"], buying, np.random.default_rng(0), main, Gate()) is None


@pytest.mark.parametrize("kind", ["synergy", "midas"])
def test_a_pair_buy_never_sells_the_partner(shop, monkeypatch, kind):
    _, main = shop
    monkeypatch.setattr(sb, "PAIRS", [("j_egg", frozenset({"j_vampire"}))])
    monkeypatch.setattr(sb, "MIDAS", "j_egg")
    game = main["info"]["game"]
    for key in ("j_vampire", "j_joker", "j_jolly", "j_zany", "j_mad"):
        game.debug_add(key)
    refreshed(main)
    p = sb.propose(kind, main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    sold = [game.jokers[m.entity_target].key for m in p.moves if int(m.action_type) == sb.SELL_JOKER]
    assert len(sold) == 1 and sold != ["j_vampire"]
    assert int(p.moves[-1].action_type) == sb.BUY


def test_a_buy_against_a_held_joker_is_vetoed(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "CONFLICTS", {"j_egg": frozenset({"j_joker"})})
    game = main["info"]["game"]
    buying = FactoredAction(sb.BUY, entity_target=index_of(game, "j_egg"))
    assert sb.propose("conflict", main["info"], buying, np.random.default_rng(0), main, Gate()) is None
    game.debug_add("j_joker")
    refreshed(main)
    egg = index_of(game, "j_egg")
    buying = FactoredAction(sb.BUY, entity_target=egg)
    p = sb.propose("conflict", main["info"], buying, np.random.default_rng(0), main, Gate())
    assert p.moves == [] and p.veto == game.shop_jokers[egg].sort_id


def test_a_passed_over_joker_buy_is_vetoed(shop, monkeypatch):
    _, main = shop
    game = main["info"]["game"]
    egg = index_of(game, "j_egg")
    buying = FactoredAction(sb.BUY, entity_target=egg)
    assert sb.propose("pass joker", main["info"], buying, np.random.default_rng(0), main, Gate()) is None
    monkeypatch.setattr(sb, "PASSED_OVER", frozenset({"j_egg"}))
    assert sb.propose("pass joker", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None
    closed = Gate(False)
    assert sb.propose("pass joker", main["info"], buying, np.random.default_rng(0), main, closed) is None
    assert closed.asked == 1
    p = sb.propose("pass joker", main["info"], buying, np.random.default_rng(0), main, Gate())
    assert p.moves == [] and p.veto == game.shop_jokers[egg].sort_id
    view = sb.without(env_view(main["info"]), game, p.veto)
    assert not view.mask.entity_masks[ActionType.BuyCard][egg]


def own(main: dict, *vouchers: str) -> None:
    """Mark ``vouchers`` owned on the line's game."""
    game = main["info"]["game"]
    snapshot = game.to_dict()
    snapshot["sets"]["used_vouchers"] = list(vouchers)
    game.replace_state(snapshot)
    refreshed(main)


def test_voucher_tables_name_real_cards():
    from jackdaw.engine.data import load
    centers = load("centers")
    for table in (sb.VOUCHER_PAIRS, sb.VOUCHER_CONFLICTS):
        for vouchers, jokers in table:
            assert all(v.startswith("v_") for v in vouchers) and all(j.startswith("j_") for j in jokers)
            assert vouchers | jokers <= set(centers)


def test_linked_reads_a_voucher_table_either_way_round():
    table = [(frozenset({"v_observatory"}), frozenset({"j_perkeo"}))]
    assert sb.linked("v_observatory", {"j_perkeo", "j_joker"}, table) == {"j_perkeo"}
    assert sb.linked("j_perkeo", {"v_observatory"}, table) == {"v_observatory"}
    assert sb.linked("j_perkeo", {"v_telescope"}, table) == frozenset()


def test_a_voucher_that_feeds_a_held_joker_is_redeemed(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "VOUCHER_PAIRS", [(frozenset({"v_hone"}), frozenset({"j_joker"}))])
    game = main["info"]["game"]
    hone = FactoredAction(sb.REDEEM, entity_target=0)
    assert game.shop_vouchers[0].key == "v_hone"
    assert sb.propose("voucher synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None
    game.debug_add("j_joker")
    refreshed(main)
    p = sb.propose("voucher synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p.moves == [hone]
    assert sb.propose("voucher synergy", main["info"], hone, np.random.default_rng(0), main, Gate()) is None
    main["info"]["game"].debug_set(money=9)
    main["info"]["game"].debug_add("c_pluto")
    refreshed(main)
    p = sb.propose("voucher synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert [int(m.action_type) for m in p.moves] == [sb.SELL_CONS, sb.REDEEM]
    main["info"]["game"].debug_set(money=0)
    refreshed(main)
    assert sb.propose("voucher synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None


def test_a_joker_beside_an_owned_voucher_it_pairs_with_is_bought(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "VOUCHER_PAIRS", [(frozenset({"v_hone"}), frozenset({"j_egg"}))])
    assert sb.propose("synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None
    own(main, "v_hone")
    game = main["info"]["game"]
    assert sb.owned_vouchers(game) == {"v_hone"}
    p = sb.propose("synergy", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p.moves == [FactoredAction(sb.BUY, entity_target=index_of(game, "j_egg"))]


def test_a_voucher_against_a_held_joker_is_vetoed(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "VOUCHER_CONFLICTS", [(frozenset({"v_hone"}), frozenset({"j_joker"}))])
    game = main["info"]["game"]
    hone = FactoredAction(sb.REDEEM, entity_target=0)
    assert sb.propose("conflict", main["info"], hone, np.random.default_rng(0), main, Gate()) is None
    game.debug_add("j_joker")
    game = refreshed(main)["info"]["game"]
    p = sb.propose("conflict", main["info"], hone, np.random.default_rng(0), main, Gate())
    assert p.moves == [] and p.veto == game.shop_vouchers[0].sort_id
    view = sb.without(env_view(main["info"]), game, p.veto)
    assert not view.mask.entity_masks[ActionType.RedeemVoucher][0]
    assert view.mask.entity_masks[ActionType.BuyCard][index_of(game, "j_egg")]


def test_a_joker_against_an_owned_voucher_is_vetoed(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "VOUCHER_CONFLICTS", [(frozenset({"v_retcon"}), frozenset({"j_egg"}))])
    game = main["info"]["game"]
    buying = FactoredAction(sb.BUY, entity_target=index_of(game, "j_egg"))
    assert sb.propose("conflict", main["info"], buying, np.random.default_rng(0), main, Gate()) is None
    own(main, "v_retcon")
    game = main["info"]["game"]
    egg = index_of(game, "j_egg")
    p = sb.propose("conflict", main["info"], FactoredAction(sb.BUY, entity_target=egg),
                   np.random.default_rng(0), main, Gate())
    assert p.veto == game.shop_jokers[egg].sort_id


def test_an_open_slot_beside_madness_tries_a_joker(shop):
    _, main = shop
    game = main["info"]["game"]
    assert sb.propose("fill slot", main["info"], LEAVE, np.random.default_rng(0), main, Gate()) is None
    game.debug_add("j_madness")
    refreshed(main)
    p = sb.propose("fill slot", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p.moves == [FactoredAction(sb.BUY, entity_target=index_of(game, "j_egg"))]
    stay = FactoredAction(int(ActionType.Reroll))
    assert sb.propose("fill slot", main["info"], stay, np.random.default_rng(0), main, Gate()) is None


def test_the_best_joker_needs_its_gain(shop, monkeypatch):
    _, main = shop
    monkeypatch.setattr(sb, "GAIN", -1.0)
    p = sb.propose("best joker", main["info"], LEAVE, np.random.default_rng(0), main, Gate())
    assert p is not None and int(p.moves[-1].action_type) == sb.BUY
    monkeypatch.setattr(sb, "GAIN", 1e9)
    gate = Gate()
    assert sb.propose("best joker", main["info"], LEAVE, np.random.default_rng(0), main, gate) is None
    assert gate.asked == 1


def test_an_enhancing_tarot_held_through_a_shop_is_tried_at_the_next_blind(shop):
    bp, main = shop
    game = main["info"]["game"]
    game.debug_add("c_magician")
    refreshed(main)
    sb.note_held(game, main)
    for move in (ActionType.NextRound, ActionType.SelectBlind):
        bp._step(main, FactoredAction(int(move)))
    play = FactoredAction(int(ActionType.PlayHand), card_target=(0,))
    assert sb.propose("held tarot", main["info"], play, None, main, Gate()) is None  # bought this visit
    main["shops"] += 1
    p = sb.propose("held tarot", main["info"], play, None, main, Gate())
    assert p.moves == [FactoredAction(sb.USE, entity_target=0)]


def test_a_branch_makes_its_sale_then_its_buy(shop, model, monkeypatch):
    bp, main = shop
    monkeypatch.setattr(sb, "COPY_JOKERS", frozenset({"j_egg"}))
    model = copy.deepcopy(model)
    with torch.no_grad():
        model.type_head.bias[sb.LEAVE] += 30.0
    game = main["info"]["game"]
    game.debug_set(money=3)
    for key in ("j_joker", "j_jolly", "j_zany", "j_mad", "j_crazy"):
        game.debug_add(key)
    refreshed(main)
    bp._tick(model)
    line, = [l for l in bp.lines if l["role"] == "branch"]
    assert line["decision"]["shop"] == "copy joker"
    assert [int(m.action_type) for m in line["force"]] == [sb.SELL_JOKER, sb.BUY]
    bp._tick(model)  # the sale
    assert len(line["info"]["game"].jokers) == 4
    bp._tick(model)  # the buy
    assert "j_egg" in [j.key for j in line["info"]["game"].jokers]
    assert "j_egg" not in [j.key for j in main["info"]["game"].jokers]
    (view, buy, buy_logp), = line["decision"]["follow"]
    assert int(buy.action_type) == sb.BUY and buy_logp < 0

    decision = dict(line["decision"])
    result = {"cut": None, "ante": 3, "won": False}
    bp._resolve(decision, {**result, "points": 5.0}, {**result, "points": 2.0})
    ex, logp, adv = branch._pair_examples(bp._pairs, branch.DECK, branch.STAKE)
    assert len(ex) == 3 and int(ex.action(2).action_type) == sb.BUY
    assert adv.tolist() == [1.5, -1.5, -1.5] and np.isclose(logp[2], buy_logp)
    with torch.no_grad():
        again = scored(model, Prepared(ex), np.arange(3))[0].numpy()
    assert np.allclose(again, logp, atol=1e-5)
    assert "shop copy joker" in branch.tally([dict(line["decision"], tied=False, joined=False, cut=False,
                                                    diff=0.0, **{"ante diff": 0, "win diff": 0})])


def test_a_voucher_branch_redeems_the_voucher(shop, model, monkeypatch):
    bp, main = shop
    monkeypatch.setattr(sb, "VOUCHER_PAIRS", [(frozenset({"v_hone"}), frozenset({"j_joker"}))])
    monkeypatch.setattr(sb, "KINDS", ("voucher synergy",))
    model = copy.deepcopy(model)
    with torch.no_grad():
        model.type_head.bias[sb.LEAVE] += 30.0
    main["info"]["game"].debug_add("j_joker")
    refreshed(main)
    bp._tick(model)
    line, = [l for l in bp.lines if l["role"] == "branch"]
    assert line["decision"]["shop"] == "voucher synergy"
    bp._tick(model)
    assert sb.owned_vouchers(line["info"]["game"]) == {"v_hone"}
    assert sb.owned_vouchers(main["info"]["game"]) == frozenset()


def test_a_vetoed_branch_never_buys_the_planet(shop, model, monkeypatch):
    bp, main = shop
    monkeypatch.setattr(sb, "MIN_PLAYS", 1)
    monkeypatch.setattr(sb, "KINDS", ("off-build planet",))
    monkeypatch.setitem(sb.SHOP_CHANCE, "off-build planet", 1.0)
    model = copy.deepcopy(model)
    with torch.no_grad():
        model.type_head.bias[sb.BUY] += 30.0
    game = main["info"]["game"]
    mars = index_of(game, "c_mars")
    buying = FactoredAction(sb.BUY, entity_target=mars)
    views = [env_view(main["info"])]
    prep = Prepared(dataset.examples_of(views))
    (alt, alt_logp, rest, veto, kind), = bp._shop(model, prep, views, [buying], set()).values()
    assert kind == "off-build planet" and veto == game.shop_jokers[mars].sort_id and rest == []
    assert not branch.same_move(alt, buying)
    line = bp._branch(main, views[0], buying, 0.0, alt, alt_logp, rest=rest, veto=veto, shop=kind)
    bp.lines = [line]
    for _ in range(6):
        if line not in bp.lines or line["info"]["game"].phase.value != "shop":
            break
        bp._tick(model)
    assert "c_mars" not in [c.key for c in line["info"]["game"].consumables]


def test_a_lineup_is_scored_with_the_next_boss_disabled(shop):
    _, main = shop
    game = main["info"]["game"]
    snap = game.to_dict()
    snap["round_resets"]["blind_choices"]["Big"] = "bl_final_bell"
    game.replace_state(snap)
    ahead = game.clone()
    ahead.step(sb.A.NextRound())
    ahead.step(sb.A.SelectBlind())
    assert ahead.blind.key == "bl_final_bell"
    hands = sb.dealt_hands(game, main["info"]["state"], np.random.default_rng(0))
    assert sb.hand_score(game, hands) > 0


def test_buy_plans_never_sell_a_protected_joker(shop):
    _, main = shop
    game = main["info"]["game"]
    for key in ("j_hologram", "j_constellation", "j_joker", "j_jolly", "j_zany"):
        game.debug_add(key)
    refreshed(main)
    game = main["info"]["game"]
    plans = sb.buy_plans(game, main["info"]["state"], main["info"]["action_mask"],
                         index_of(game, "j_egg"))
    sold = {game.jokers[m.entity_target].key for p in plans for m in p
            if int(m.action_type) == sb.SELL_JOKER}
    assert sold and not sold & sb.SELL_PROTECTED


@pytest.mark.parametrize("lineup, copier, swap", [
    (("j_joker", "j_blueprint"), "j_blueprint", FactoredAction(sb.SWAP_LEFT, entity_target=1)),
    (("j_egg", "j_cavendish", "j_brainstorm"), "j_brainstorm",
     FactoredAction(sb.SWAP_LEFT, entity_target=1)),
])
def test_a_copy_joker_is_moved_to_where_it_copies(shop, lineup, copier, swap):
    _, main = shop
    game = main["info"]["game"]
    for key in lineup:
        game.debug_add(key)
    refreshed(main)
    game = main["info"]["game"]
    sort_id = next(j.sort_id for j in game.jokers if j.key == copier)
    hands = sb.dealt_hands(game, main["info"]["state"], np.random.default_rng(0))
    assert sb.placed(game, [], hands, sort_id) == [swap]
    assert sb.placed(game, [swap], hands, sort_id) == [swap]


def card(front="H_7", key="c_base", seal=None, edition=None, face_down=False):
    from types import SimpleNamespace
    return SimpleNamespace(front=front, key=key, seal=seal, edition=edition, face_down=face_down)


def test_a_red_seal_is_taken_over_any_other_pick():
    rng = np.random.default_rng(0)
    cards = [card(), card(key="m_glass", seal="Gold"), card(seal="Red")]
    assert sb.upgraded_pick("red seal", cards, 1, rng) == 2
    assert sb.upgraded_pick("red seal", cards, 2, rng) is None
    assert sb.upgraded_pick("red seal", [card(), card(seal="Red", face_down=True)], 0, rng) is None


def test_a_plain_pick_tries_a_sealed_card_first():
    rng = np.random.default_rng(0)
    cards = [card(), card(key="m_steel"), card(seal="Blue"), card(edition="foil")]
    assert sb.upgraded_pick("upgraded card", cards, 0, rng) == 2
    assert sb.upgraded_pick("upgraded card", [card(), card(key="m_steel"), None], 0, rng) == 1
    assert sb.upgraded_pick("upgraded card", cards, 1, rng) is None
    assert sb.upgraded_pick("upgraded card", [card(), card()], 0, rng) is None
    assert sb.upgraded_pick("upgraded card", [card(front=""), card(seal="Red")], 0, rng) is None


def test_a_plain_card_from_a_standard_pack_branches_on_an_upgraded_one(shop):
    from jackdaw.engine import actions as A
    _, main = shop
    game = main["info"]["game"]
    game.debug_set(money=100)
    game.debug_add("p_standard_mega_1")
    refreshed(main)
    game = main["info"]["game"]
    at = [b.key for b in game.shop_boosters].index("p_standard_mega_1")
    main["env"].step(FactoredAction(int(ActionType.OpenBooster), entity_target=at))
    refreshed(main)
    game = main["info"]["game"]
    plain = [i for i, c in enumerate(game.pack_cards) if not sb.upgraded(c)]
    better = {i for i, c in enumerate(game.pack_cards) if sb.upgraded(c)}
    assert plain and better
    pick = FactoredAction(sb.PICK, entity_target=plain[0])
    p = sb.propose("upgraded card", main["info"], pick, np.random.default_rng(0), main, Gate())
    assert int(p.moves[0].action_type) == sb.PICK and p.moves[0].entity_target in better
    assert sb.occasion("upgraded card", main)[-1] == tuple(c.sort_id for c in game.pack_cards)
