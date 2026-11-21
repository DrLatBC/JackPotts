"""What a learning run starts from, and the check against that start when it ends."""

from __future__ import annotations

import json

import pytest
import torch

from rl import ppo
from rl.evaluate import DATA, ROOT, model_file
from rl.model import Policy


def test_a_start_is_a_copy_by_name_or_a_save_by_path(tmp_path):
    assert model_file("b187") == DATA / "models" / "b187.pt"
    assert model_file("rl/data/ppo/s2-steps/u00019.pt") == ROOT / "rl" / "data" / "ppo" / "s2-steps" / "u00019.pt"
    absolute = tmp_path / "x.pt"
    assert model_file(str(absolute)) == absolute


@pytest.fixture
def check_run(tmp_path, monkeypatch):
    """A run folder, a status that goes nowhere, and play() that scores each seed by
    which model played it, counting the games: each wins the first seed and plays it on
    into endless mode, the model to ante 12, the start to ante 10."""
    monkeypatch.setattr(ppo, "report", lambda status, **changes: status.update(changes))
    calls = []
    torch.manual_seed(0)
    start, model = Policy(width=32, layers=1), Policy(width=32, layers=1)

    def play(policy, seeds, endless=False):
        assert endless
        calls.append((policy, len(seeds)))
        ante = 5 if policy is model else 3
        rows = [{"seed": s, "won": False, "ante": ante, "steps": 1, "cut": None} for s, _, _ in seeds]
        rows[0].update({"won": True, "ante": 9, "endless ante": 12 if policy is model else 10,
                        "endless cut": None})
        return rows

    monkeypatch.setattr(ppo, "play", play)
    return tmp_path, start, model, calls


def test_the_final_check_plays_the_start_once_and_compares_seed_by_seed(check_run):
    out, start, model, calls = check_run
    status = {}
    s = ppo.final_check(model, start, out, {"updates": 19}, 6, status)
    assert [(p is start, n) for p, n in calls] == [(True, 6), (False, 6)]
    assert s["vs start"]["seeds"] == 6 and s["vs start"]["ante gain"] == 2 * 5 / 6
    assert s["vs start"]["endless gain"] == 2 and s["endless ante"] == 12
    saved = json.loads((out / "check_u00019.json").read_text())
    assert saved["summary"]["vs start"]["endless gain"] == 2 and len(saved["games"]) == 6
    assert status["check"]["update"] == 19 and status["doing"] == ""

    calls.clear()
    ppo.final_check(model, start, out, {"updates": 40}, 6, status)
    assert [p is start for p, _ in calls] == [False]  # the start's games are kept
    assert (out / "check_u00040.json").exists() and status["check"]["update"] == 40


def test_a_kept_start_check_whose_wins_stopped_at_the_win_is_played_again(check_run):
    out, start, model, calls = check_run
    old = [{"seed": "RLE-000001", "won": True, "ante": 9, "steps": 1, "cut": None}]
    (out / "check_start_6.json").write_text(json.dumps({"summary": {}, "games": old}))
    ppo.final_check(model, start, out, {"updates": 3}, 6, {})
    assert [p is start for p, _ in calls] == [True, False]
    kept = json.loads((out / "check_start_6.json").read_text())["games"]
    assert kept[0]["endless ante"] == 10
