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
    which model played it, counting the games."""
    monkeypatch.setattr(ppo, "report", lambda status, **changes: status.update(changes))
    calls = []
    torch.manual_seed(0)
    start, model = Policy(width=32, layers=1), Policy(width=32, layers=1)

    def play(policy, seeds):
        calls.append((policy, len(seeds)))
        ante = 5 if policy is model else 3
        return [{"seed": s, "won": False, "ante": ante, "steps": 1, "cut": None} for s, _, _ in seeds]

    monkeypatch.setattr(ppo, "play", play)
    return tmp_path, start, model, calls


def test_the_final_check_plays_the_start_once_and_compares_seed_by_seed(check_run):
    out, start, model, calls = check_run
    status = {}
    s = ppo.final_check(model, start, out, {"updates": 19}, 6, status)
    assert [(p is start, n) for p, n in calls] == [(True, 6), (False, 6)]
    assert s["vs start"]["seeds"] == 6 and s["vs start"]["ante gain"] == 2
    saved = json.loads((out / "check_u00019.json").read_text())
    assert saved["summary"]["vs start"]["ante gain"] == 2 and len(saved["games"]) == 6
    assert status["check"]["update"] == 19 and status["doing"] == ""

    calls.clear()
    ppo.final_check(model, start, out, {"updates": 40}, 6, status)
    assert [p is start for p, _ in calls] == [False]  # the start's games are kept
    assert (out / "check_u00040.json").exists() and status["check"]["update"] == 40
