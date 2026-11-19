"""Game servers on their own: the interpreter rule, stall timeouts, version tags, the
commands each adapter starts, the profile it plays in and the health reply."""

import socket
import zipfile
import zlib

import pytest

from balatro_bot import game_server
from balatro_bot.config import venv_python
from balatro_bot.game_server import Jackdaw, LiveGame, read_health


@pytest.fixture
def install(monkeypatch):
    for path in ("love_path", "lovely_path", "balatrobot_path"):
        monkeypatch.setitem(game_server.INSTALL.__dict__, path, path)


def test_simulator_bots_run_on_pypy_when_its_environment_exists(tmp_path, monkeypatch):
    pypy = tmp_path / "pypy.exe"
    pypy.write_text("")
    monkeypatch.setenv("BOT_PYPY", str(pypy))
    assert Jackdaw().bot_python(kernel=False) == str(pypy)
    monkeypatch.setenv("BOT_PYPY", str(tmp_path / "missing.exe"))
    assert Jackdaw().bot_python(kernel=False) == venv_python("bot")


def test_simulator_bots_stay_on_cpython_with_the_compiled_kernel(tmp_path, monkeypatch):
    pypy = tmp_path / "pypy.exe"
    pypy.write_text("")
    monkeypatch.setenv("BOT_PYPY", str(pypy))
    assert Jackdaw().bot_python(kernel=True) == venv_python("bot")


def test_live_game_bots_run_on_cpython(tmp_path, monkeypatch):
    pypy = tmp_path / "pypy.exe"
    pypy.write_text("")
    monkeypatch.setenv("BOT_PYPY", str(pypy))
    assert LiveGame().bot_python(kernel=False) == LiveGame().bot_python(kernel=True) == venv_python("bot")


def test_a_stuck_bot_is_given_ten_minutes_live_and_one_on_the_simulator():
    assert (LiveGame.stall_timeout, Jackdaw.stall_timeout) == (600, 60)


def test_simulator_batches_tag_their_code_version():
    assert (LiveGame.version_tag, Jackdaw.version_tag) == ("", "+jackdaw")


def test_a_batch_game_is_headless_and_fast(install):
    assert LiveGame().command(12346) == ["balatrobot_path", "serve", "--port", "12346", "--love-path",
                                         "love_path", "--lovely-path", "lovely_path", "--headless", "--fast"]
    assert "--headless" not in LiveGame(visible=True).command(12346)


def test_each_live_port_plays_in_its_own_profile(tmp_path, monkeypatch, install):
    unlocked = []
    monkeypatch.setattr(game_server, "ensure_isolated_profile", lambda port: str(tmp_path / str(port)))
    monkeypatch.setattr(game_server, "unlock_all", lambda folder, love: unlocked.append((folder, love)))
    assert LiveGame().environment(12346)["APPDATA"] == str(tmp_path / "12346")
    assert not unlocked
    LiveGame(visible=True, profile="unlocked").environment(12340)
    assert unlocked == [(tmp_path / "12340" / "Balatro" / "1", "love_path")]


def test_a_live_game_with_a_log_writes_its_output_there(tmp_path, monkeypatch, install):
    seen = {}
    monkeypatch.setattr(game_server, "ensure_isolated_profile", lambda port: str(tmp_path))
    monkeypatch.setattr(game_server.subprocess, "Popen", lambda cmd, **kw: seen.update(kw, cmd=cmd))
    LiveGame(visible=True, log=tmp_path / "game.log").start(12340, creationflags=8)
    assert seen["stdout"].name == str(tmp_path / "game.log") and seen["stdout"].closed
    assert seen["stderr"] == game_server.subprocess.STDOUT and seen["creationflags"] == 8
    assert seen["env"]["APPDATA"] == str(tmp_path)


def test_the_simulator_serves_the_adapter_in_isolated_mode():
    cmd = Jackdaw(python="jackdaw-python").command(12371)
    assert cmd == ["jackdaw-python", "-I", str(game_server.JACKDAW_ADAPTER), "--port", "12371"]
    assert game_server.JACKDAW_ADAPTER.name == "serve.py" and game_server.JACKDAW_ADAPTER.exists()
    assert not Jackdaw(python="missing-python").available()


def test_health_is_whether_the_port_accepts_connections():
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert LiveGame().health(port) and Jackdaw().health(port)
    assert not LiveGame().health(port)


def test_only_the_simulator_says_its_responses_are_settled():
    jackdaw = read_health({"status": "ok", "simulator": "jackdaw", "simulator_version": "0.2.6"})
    assert (jackdaw.settled, jackdaw.simulator) == (True, "jackdaw 0.2.6")
    assert read_health({"status": "ok", "simulator": "jackdaw"}).simulator == "jackdaw"
    live = read_health({"status": "ok"})
    assert (live.settled, live.simulator) == (False, "")
    assert not read_health(None).settled


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
    assert game_server.game_items(love) == ["c_base", "j_joker", "j_hit_the_road", "b_blue", "bl_small",
                                       "tag_rare", "Gold", "Red"]


def test_unlock_all_writes_what_the_games_unlock_all_leaves(love, tmp_path):
    profile = tmp_path / "1"
    profile.mkdir()
    stats = 'return {["career_stats"]={["c_wins"]=3,},["all_unlocked"]=false,}'
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    (profile / "profile.jkr").write_bytes(packer.compress(stats.encode()) + packer.flush())
    game_server.unlock_all(profile, love)
    meta = read_jkr(profile / "meta.jkr")
    for part in ("unlocked", "discovered", "alerted"):
        assert f'["{part}"]={{' in meta
    assert meta.count('["j_hit_the_road"]=true') == 3 and "j_testing" not in meta
    text = read_jkr(profile / "profile.jkr")
    assert text.startswith('return {["all_unlocked"]=true,') and '["c_wins"]=3' in text
    assert '["all_unlocked"]=false' not in text
    game_server.unlock_all(profile, love)
    assert read_jkr(profile / "profile.jkr") == text


def test_unlock_all_makes_a_profile_from_nothing(love, tmp_path):
    game_server.unlock_all(tmp_path / "new" / "1", love)
    assert read_jkr(tmp_path / "new" / "1" / "profile.jkr") == 'return {["all_unlocked"]=true,}'
