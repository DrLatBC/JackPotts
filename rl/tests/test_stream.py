"""The stream player, with Jackdaw's simulator standing in for the live game."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import torch

from jackdaw.bridge import RPCError, SimBackend
from jackdaw.engine import actions as A
from jackdaw.env.action_space import ActionType

from rl import stream
from fork_view import augment, strip  # noqa: E402  (on the path once rl.stream is imported)
from rl.evaluate import DECK, STAKE, play
from rl.train import load_model

COPY = Path(__file__).resolve().parents[1] / "data" / "models" / "b187.pt"
pytestmark = pytest.mark.skipif(not COPY.exists(), reason="needs the b187 copy")
SEEDS = [f"RLE-{i:06d}" for i in range(1, 6)]


@pytest.fixture(autouse=True)
def one_thread():
    torch.set_num_threads(1)


def player(tmp_path, backend=None, **kw):
    kw.setdefault("seeds", SEEDS)
    return stream.Player(backend or SimBackend(), COPY, delay=0, out=tmp_path, **kw)


def test_the_player_writes_into_the_stream_job_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(stream.jobs, "ROOT", tmp_path)
    p = stream.Player(SimBackend(), COPY, delay=0)
    assert p.out == stream.jobs.stream().folder
    stream.jobs.stream().ask("stop")
    assert p.stop_asked()


def test_whole_games_are_recorded(tmp_path):
    p = player(tmp_path)
    assert p.run(3) == 3
    games = [json.loads(line) for line in (tmp_path / "games.jsonl").read_text().splitlines()]
    assert [g["seed"] for g in games] == SEEDS[:3]
    assert all(g["outcome"] in ("won", "lost") and g["steps"] > 0 and 1 <= g["ante"] <= 9 for g in games)
    now = json.loads((tmp_path / "now.json").read_text())
    assert now["games"] == 3 and now["checkpoint"] == "b187"
    assert [r["seed"] for r in now["recent"]] == SEEDS[:3][::-1]
    assert now["game"]["seed"] == SEEDS[2] and now["game"]["last"]


def test_hands_and_the_mean_ante_are_counted_for_the_overlay(tmp_path):
    p = player(tmp_path)
    p.run(2)
    games = [json.loads(line) for line in (tmp_path / "games.jsonl").read_text().splitlines()]
    now = json.loads((tmp_path / "now.json").read_text())
    assert all(g["hands"] > 0 for g in games)
    assert now["hands"] == sum(g["hands"] for g in games)
    assert now["game"]["hands played"] == games[-1]["hands"]
    assert now["mean ante"] == round(sum(g["ante"] for g in games) / 2, 2)


def test_the_deck_view_splits_each_card_into_to_draw_in_hand_and_spent(tmp_path):
    p = player(tmp_path)
    p.load()
    p.adapter.start("BLUE", "WHITE", SEEDS[0])
    fresh = stream.deck_view(p.adapter.game)
    assert fresh["size"] == 52 and len(fresh["cards"]) == 52
    assert all(c == {"n": 1, "draw": 1, "hand": 0, "debuff": 0} for c in fresh["cards"].values())
    while not p.adapter.game.discard_pile:
        p.move(p.pick(True, set()))
    g = p.adapter.game
    deck = stream.deck_view(g)
    cells = deck["cards"].values()
    assert deck["size"] == len(g.deck) + len(g.hand) + len(g.discard_pile) == 52
    assert sum(c["draw"] for c in cells) == deck["to draw"] == len(g.deck)
    assert sum(c["hand"] for c in cells) == len(g.hand)
    assert sum(c["n"] - c["draw"] - c["hand"] for c in cells) == len(g.discard_pile)
    assert {"SA", "H10", "DJ", "C2"} <= set(deck["cards"])


def test_debuffed_cards_are_marked_in_the_deck_and_the_cards_weighed():
    from types import SimpleNamespace

    def card(front, rank, suit, debuff, key="c_base"):
        return SimpleNamespace(front=front, rank=rank, suit=suit, key=key, debuff=debuff, face_down=False)

    clubs = [card("C_K", "King", "Clubs", True), card("C_K", "King", "Clubs", False)]
    game = SimpleNamespace(deck=[clubs[0], card("H_2", "2", "Hearts", False)], hand=[clubs[1]],
                           discard_pile=[card("C_5", "5", "Clubs", True, key="m_stone")])
    deck = stream.deck_view(game)
    assert deck["cards"]["CK"] == {"n": 2, "draw": 1, "hand": 1, "debuff": 1}
    assert deck["cards"]["H2"]["debuff"] == 0
    assert stream.card_name(clubs[0]) == {"rank": "K", "suit": "♣", "debuff": True}
    assert stream.card_name(clubs[1]) == {"rank": "K", "suit": "♣"}
    assert stream.card_name(game.discard_pile[0]) == {"rank": "Stone", "debuff": True}


def test_the_stream_plays_as_evaluation_does(tmp_path):
    """Same copy, same seeds: the stream's games end where evaluation's do (seeds where
    the live game's rules and the simulator's agree on every move)."""
    model, _ = load_model(COPY)
    model.eval()
    evaluated = {r["seed"]: r["ante"] for r in play(model, [(s, DECK, STAKE) for s in SEEDS])}
    p = player(tmp_path)
    p.run(len(SEEDS))
    assert {r["seed"]: r["ante"] for r in p.recent} == evaluated


def test_a_stop_ends_the_game_between_moves(tmp_path):
    p = player(tmp_path)
    steps = []

    def move(factored, original=p.move):
        original(factored)
        steps.append(1)
        if len(steps) == 5:
            p.job.ask("stop")

    p.move = move
    assert p.run() == 1
    assert p.recent[0]["outcome"] == "stopped" and p.recent[0]["steps"] == 5
    assert not (tmp_path / "games.jsonl").exists()
    assert json.loads((tmp_path / "now.json").read_text())["games"] == 0


def test_without_takes_refused_moves_out_of_the_mask(tmp_path):
    p = player(tmp_path)
    p.load()
    p.adapter.start("BLUE", "WHITE", SEEDS[0])
    v = stream.view(p.adapter.game, True)
    select = int(ActionType.SelectBlind)
    assert v.mask.type_mask[select]
    cut = stream.without(v, {(select, None)})
    assert not cut.mask.type_mask[select] and v.mask.type_mask[select]
    assert p.pick(True, {(select, None)}).action_type != select


def test_a_refused_move_is_replaced_by_the_next_choice(tmp_path):
    p = player(tmp_path, seeds=SEEDS[:1])
    refused = []

    def move(factored, original=p.move):
        if factored.action_type == ActionType.SelectBlind and not refused:
            refused.append(factored)
            raise stream.IllegalActionError("refused for the test")
        original(factored)

    p.move = move
    p.run(1)
    assert refused and p.recent[0]["outcome"] in ("won", "lost")


class Refusing(SimBackend):
    def handle(self, method, params):
        if method == "start":
            raise RPCError(-32001, "no game today", {"name": "INVALID_STATE"})
        return super().handle(method, params)


def test_errors_end_the_game_and_three_in_a_row_end_the_stream(tmp_path):
    p = player(tmp_path, backend=Refusing(), seeds=SEEDS * 2)
    with pytest.raises(SystemExit, match="3 games in a row"):
        p.run()
    assert [r["outcome"] for r in p.recent] == ["error"] * 3


def test_following_a_run_reads_its_latest_save_before_each_game(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    shutil.copy(COPY, run / "latest.pt")
    p = stream.Player(SimBackend(), run / "latest.pt", follow=True, delay=0, out=tmp_path, seeds=SEEDS)
    loads = []

    def load_model(path, original=stream.load_model):
        loads.append(path)
        return original(path)

    stream.load_model, saved = load_model, stream.load_model
    try:
        p.run(2)
    finally:
        stream.load_model = saved
    assert len(loads) == 2
    fixed = player(tmp_path)
    stream.load_model = load_model
    try:
        loads.clear()
        fixed.run(2)
    finally:
        stream.load_model = saved
    assert len(loads) == 1


def test_a_play_goes_in_the_bots_order_as_single_swaps(tmp_path):
    """With an order joker the stream reorders through the live game before the play,
    and ends where rl.ordering.put_in_order's play ends on a plain game."""
    from jackdaw.env.game_spec import FactoredAction

    from rl.ordering import order_can_change, put_in_order

    p = player(tmp_path)
    p.load()
    p.adapter.start("BLUE", "WHITE", SEEDS[0])
    p.adapter.step(A.SelectBlind())
    p.adapter.request("add", {"key": "j_photograph"})
    plain = p.adapter.game.clone()
    cards = (len(plain.hand) - 3, len(plain.hand) - 2, len(plain.hand) - 1)
    assert order_can_change(plain, cards)
    plain.step(A.PlayHand(put_in_order(plain, cards)))

    sent = []
    step = p.adapter.step
    p.adapter.step = lambda action: sent.append(action) or step(action)
    p.move(FactoredAction(int(ActionType.PlayHand), card_target=cards))
    assert isinstance(sent[-1], A.PlayHand)
    assert all(isinstance(a, A.SwapHandLeft) for a in sent[:-1])
    assert p.adapter.game.state_hash() == plain.state_hash()


def test_describe_names_the_move():
    assert stream.describe(A.PlayHand((0, 2, 4))) == "PlayHand 0 2 4"
    assert stream.describe(A.CashOut()) == "CashOut"
    assert stream.describe(A.BuyCard(1)) == "BuyCard 1"


def test_games_that_diverge_before_their_first_move_end_the_stream(tmp_path, monkeypatch):
    p = player(tmp_path, seeds=SEEDS * 2)
    early = {"outcome": "divergence", "steps": 0, "error": "lockstep divergence at step 0"}
    monkeypatch.setattr(p, "play_game", lambda: dict(early, seed="s"))
    with pytest.raises(SystemExit, match="3 games in a row ended before their first move"):
        p.run()


def test_games_that_diverge_later_play_on(tmp_path, monkeypatch):
    p = player(tmp_path)
    monkeypatch.setattr(p, "play_game", lambda: {"outcome": "divergence", "steps": 40, "seed": "s",
                                                 "error": "lockstep divergence at step 40"})
    assert p.run(5) == 5


class ForkSim(SimBackend):
    """The simulator answering with the JackPotts fork's extra fields, as the real game
    and ``sim/jackdaw_adapter/serve.py`` do."""

    def handle(self, method, params):
        result = super().handle(method, params)
        return augment(result, self.game.to_dict() if self.game is not None else None)


def test_the_forks_extra_fields_alone_diverge_at_the_start(tmp_path):
    p = player(tmp_path, backend=ForkSim(), seeds=SEEDS[:1])
    p.run(1)
    assert p.recent[0]["outcome"] == "divergence" and p.recent[0]["steps"] == 0


def test_without_the_forks_fields_the_stream_plays_in_step(tmp_path):
    plain = player(tmp_path / "plain")
    plain.run(len(SEEDS))
    fork = player(tmp_path / "fork", backend=stream.UpstreamView(ForkSim()))
    fork.run(len(SEEDS))
    assert [(r["outcome"], r["ante"], r["steps"]) for r in fork.recent] == \
           [(r["outcome"], r["ante"], r["steps"]) for r in plain.recent]


def test_strip_undoes_augment_through_a_whole_game(tmp_path):
    """Every answer of a played game: the fork's view with its fields taken out is the
    upstream view again."""
    import copy

    sim = SimBackend()
    answers = []
    handle = sim.handle

    def record(method, params):
        result = handle(method, params)
        answers.append((copy.deepcopy(result), sim.game.to_dict() if sim.game is not None else None))
        return result

    sim.handle = record
    player(tmp_path, backend=sim, seeds=SEEDS[:2]).run(2)
    assert len(answers) > 50
    for upstream, snap in answers:
        forked = augment(copy.deepcopy(upstream), snap)
        assert strip(forked) == upstream


def test_a_status_write_that_fails_never_ends_a_game(tmp_path, monkeypatch):
    def busy(tmp, path):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(stream.jobs, "replace_file", busy)
    p = player(tmp_path, seeds=SEEDS[:1])
    p.run(1)
    assert p.recent[0]["outcome"] in ("won", "lost")


def test_weigh_leads_with_the_move_choose_picks(tmp_path):
    from rl import dataset
    from rl.model import Prepared, choose, weigh

    p = player(tmp_path)
    p.load()
    p.adapter.start("BLUE", "WHITE", SEEDS[0])
    for _ in range(30):
        prep = Prepared(dataset.examples_of([stream.view(p.adapter.game, True)]))
        w = weigh(p.model, prep)
        assert w.options[0][0] == choose(p.model, prep)[0]
        assert abs(sum(w.types.values()) - 1) < 1e-5
        type_p = [w.types[a.action_type] for a, _ in w.options]
        assert type_p == sorted(type_p, reverse=True)
        assert all(0 < sure <= 1 for _, sure in w.options)
        p.move(w.options[0][0])
        if p.adapter.done:
            break


def test_now_json_shows_what_the_model_weighed(tmp_path):
    p = player(tmp_path, seeds=SEEDS[:1])
    p.run(1)
    game = json.loads((tmp_path / "now.json").read_text(encoding="utf-8"))["game"]
    assert len(game["history"]) == game["steps"]
    assert isinstance(game["outlook"], float)
    lead = game["considering"][0]
    assert lead["move"] in stream.MOVES.values() and 0 < lead["p"] <= 1 and 0 < lead["sure"] <= 1


def test_the_wait_comes_between_showing_a_move_and_making_it(tmp_path, monkeypatch):
    p = stream.Player(SimBackend(), COPY, delay=1.5, out=tmp_path, seeds=SEEDS[:1])
    events = []
    monkeypatch.setattr(stream.time, "sleep", lambda s: events.append("wait"))
    pick, move = p.pick, p.move
    p.pick = lambda *a: (events.append("show"), pick(*a))[1]

    def move_then_stop(factored):
        events.append("move")
        move(factored)
        if events.count("move") == 3:
            p.job.ask("stop")

    p.move = move_then_stop
    p.run(1)
    assert events == ["show", "wait", "move"] * 3


def test_option_names_the_cards_hand_and_item(tmp_path):
    from jackdaw.env.game_spec import FactoredAction

    p = player(tmp_path)
    p.adapter.start("BLUE", "WHITE", SEEDS[0])
    p.adapter.step(A.SelectBlind())
    p.adapter.request("add", {"key": "j_photograph"})
    game = p.adapter.game
    played = stream.option(FactoredAction(int(ActionType.PlayHand), card_target=(0, 1)), game)
    assert played["move"] == "Play" and played["hand"] == game.scoring_cards((0, 1))[0]
    first = game.hand[0]
    assert played["cards"][0] == {"rank": stream.RANKS.get(first.rank, first.rank), "suit": stream.SUITS[first.suit]}
    sold = stream.option(FactoredAction(int(ActionType.SellJoker), entity_target=0), game)
    assert sold == {"move": "Sell", "item": {"name": "Photograph"}}
    assert stream.title("bl_hook") == "The Hook"
