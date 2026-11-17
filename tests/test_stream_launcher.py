"""The stream launcher with stand-in processes: how each way a stream ends is reported."""

import json
import zipfile
import zlib
from argparse import Namespace

import pytest

from balatro_bot import stream


class FakeProc:
    """A process that prints ``output`` when started, runs ``each_poll`` whenever it is
    polled, and exits after ``polls`` polls (never, with None)."""

    def __init__(self, polls=None, code=0, output="", each_poll=None):
        self.polls, self.returncode, self.pid = polls, None, 4242
        self.code, self.output, self.each_poll = code, output, each_poll

    def poll(self):
        if self.each_poll:
            self.each_poll()
        if self.polls is not None:
            self.polls -= 1
            if self.polls <= 0:
                self.returncode = self.code
        return self.returncode


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "STREAM_DIR", tmp_path)
    monkeypatch.setattr(stream, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(stream, "STOP", tmp_path / "stop")
    monkeypatch.setattr(stream, "PLAYER_LOG", tmp_path / "stream.log")
    monkeypatch.setattr(stream, "GAME_LOG", tmp_path / "game.log")
    monkeypatch.setattr(stream, "tie_children_to_supervisor", lambda: True)
    monkeypatch.setattr(stream, "kill_port_processes", lambda ports: 0)
    monkeypatch.setattr(stream, "ensure_isolated_profile", lambda port: str(tmp_path / "profile"))
    monkeypatch.setattr(stream, "graceful_kill", lambda proc: None)
    monkeypatch.setattr(stream.time, "sleep", lambda s: None)
    monkeypatch.setattr(stream.subprocess, "run", lambda *a, **k: None)
    procs = {}

    def popen(cmd, **kw):
        kind = "player" if "rl.stream" in cmd else "game"
        procs[kind + "_cmd"] = cmd
        kw["stdout"].write(procs[kind].output)
        kw["stdout"].flush()
        return procs[kind]

    monkeypatch.setattr(stream.subprocess, "Popen", popen)
    args = Namespace(model="rl/data/ppo/r1/latest.pt", follow=True, deck="BLUE", stake="WHITE", delay=1.5,
                     port=12340, overlay_port=0)
    return tmp_path, procs, args


def status(tmp_path):
    return json.loads((tmp_path / "status.json").read_text())


def test_player_command_runs_rl_stream_under_the_rl_venv():
    args = Namespace(model="m.pt", follow=True, deck="RED", stake="GOLD", delay=0.5, port=12340)
    cmd = stream.player_command(args)
    assert ".venv-rl" in cmd[0] and cmd[1:3] == ["-m", "rl.stream"]
    assert cmd[cmd.index("--port") + 1] == "12340" and cmd[-1] == "--follow"
    assert "--follow" not in stream.player_command(Namespace(**{**vars(args), "follow": False}))


def test_a_stop_before_the_game_opens(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    monkeypatch.setattr(stream, "listening", lambda port: (tmp_path / "stop").touch())
    assert stream.run(args) == 1
    assert status(tmp_path)["phase"] == "stopped"
    assert "player_cmd" not in procs


def test_a_game_that_never_opens_fails_with_its_last_line(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc(polls=1, code=1, output="starting\nLovely: could not inject\n")
    monkeypatch.setattr(stream, "listening", lambda port: False)
    assert stream.run(args) == 1
    assert status(tmp_path)["phase"] == "failed"
    assert status(tmp_path)["error"] == "Lovely: could not inject"


def test_a_stopped_player_ends_the_stream_as_stopped(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=3, each_poll=(tmp_path / "stop").touch)
    monkeypatch.setattr(stream, "listening", lambda port: True)
    assert stream.run(args) == 0
    assert status(tmp_path)["phase"] == "stopped"
    assert not (tmp_path / "stop").exists()
    assert procs["player_cmd"][procs["player_cmd"].index("--model") + 1] == args.model


def test_a_player_that_dies_fails_the_stream(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=2, code=1, output="Traceback\nSystemExit: 3 games in a row ended in an error\n")
    monkeypatch.setattr(stream, "listening", lambda port: True)
    assert stream.run(args) == 1
    s = status(tmp_path)
    assert s["phase"] == "failed" and s["error"].startswith("SystemExit: 3 games")


def test_closing_the_game_window_stops_the_stream(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"], procs["player"] = FakeProc(polls=4), FakeProc()
    monkeypatch.setattr(stream, "listening", lambda port: True)
    assert stream.run(args) == 0
    s = status(tmp_path)
    assert s["phase"] == "stopped" and s["note"] == "the game window was closed"


GAME_LUA = '''
    self.P_SEALS = {
        Gold =      {order = 1,  discovered = false, set = "Seal"},
        Red =       {order = 2,  discovered = false, set = "Seal"},
    }
    self.P_TAGS = {
        tag_rare =          {name = 'Rare Tag', set = 'Tag', config = {type = 'store_joker_create'}, pos = {x = 1,y = 0}},
    }
    self.P_BLINDS = {
        bl_small =           {name = 'Small Blind', vars = {}, debuff = {}, pos = {x=0, y=0}},
    }
    self.P_CENTERS = {
        c_base={max = 500, name = "Default Base", pos = {x=1,y=0}, config = {}},

        --Jokers
        j_joker=            {order = 1,  unlocked = true, config = {mult = 4}, pos = {x=0,y=0}},
        j_hit_the_road=     {order = 2,  unlocked = false, config = {extra = 0.5}, pos = {x=8,y=5}},
        j_testing=          {order = 3,  wip = true, config = {}},
        b_blue=             {name = "Blue Deck", unlocked = false, config = {hands = 1}},
    }
'''


@pytest.fixture
def love(tmp_path):
    path = tmp_path / "Balatro.exe"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("game.lua", GAME_LUA)
    return str(path)


def read_jkr(path):
    raw = path.read_bytes()
    return raw.decode() if raw[:6] == b"return" else zlib.decompress(raw, -15).decode()


def test_game_items_reads_every_table_and_skips_work_in_progress(love):
    assert stream.game_items(love) == ["c_base", "j_joker", "j_hit_the_road", "b_blue", "bl_small",
                                       "tag_rare", "Gold", "Red"]


def test_unlock_all_writes_what_the_games_unlock_all_leaves(love, tmp_path):
    profile = tmp_path / "1"
    profile.mkdir()
    stats = 'return {["career_stats"]={["c_wins"]=3,},["all_unlocked"]=false,}'
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    (profile / "profile.jkr").write_bytes(packer.compress(stats.encode()) + packer.flush())
    stream.unlock_all(profile, love)
    meta = read_jkr(profile / "meta.jkr")
    for part in ("unlocked", "discovered", "alerted"):
        assert f'["{part}"]={{' in meta
    assert meta.count('["j_hit_the_road"]=true') == 3 and "j_testing" not in meta
    text = read_jkr(profile / "profile.jkr")
    assert text.startswith('return {["all_unlocked"]=true,') and '["c_wins"]=3' in text
    assert '["all_unlocked"]=false' not in text
    stream.unlock_all(profile, love)
    assert read_jkr(profile / "profile.jkr") == text


def test_unlock_all_makes_a_profile_from_nothing(love, tmp_path):
    stream.unlock_all(tmp_path / "new" / "1", love)
    assert read_jkr(tmp_path / "new" / "1" / "profile.jkr") == 'return {["all_unlocked"]=true,}'


def test_replace_file_waits_out_a_reader(tmp_path, monkeypatch):
    from balatro_bot import files

    target, tmp = tmp_path / "now.json", tmp_path / "now.tmp"
    target.write_text("old")
    tmp.write_text("new")
    real, refusals = files.os.replace, [2]

    def replace(src, dst):
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(files.os, "replace", replace)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    assert files.replace_file(tmp, target) and target.read_text() == "new"
    refusals[0] = 999
    tmp.write_text("newer")
    assert not files.replace_file(tmp, target, tries=3) and target.read_text() == "new"


def test_the_overlay_is_served_while_the_stream_runs(launcher, monkeypatch):
    import urllib.request

    tmp_path, procs, args = launcher
    (tmp_path / "now.json").unlink(missing_ok=True)
    seen = {}

    def check():
        url = status(tmp_path).get("overlay")
        if url and "page" not in seen:
            with urllib.request.urlopen(url, timeout=5) as r:
                seen["page"] = r.status
            (tmp_path / "stop").touch()

    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=5, each_poll=check)
    monkeypatch.setattr(stream, "listening", lambda port: True)
    assert stream.run(args) == 0
    assert seen["page"] == 200
    with pytest.raises(OSError):
        urllib.request.urlopen(status(tmp_path)["overlay"], timeout=2)


def test_a_taken_overlay_port_leaves_the_stream_running(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=3, each_poll=(tmp_path / "stop").touch)
    monkeypatch.setattr(stream, "listening", lambda port: True)

    def taken(folder, port):
        raise OSError("in use")

    monkeypatch.setattr(stream.overlay, "serve", taken)
    assert stream.run(args) == 0
    s = status(tmp_path)
    assert s["phase"] == "stopped" and "overlay" not in s and "in use" in s["overlay error"]
