"""The overlay server: the page, the stream's files, and nothing else."""

import json
import urllib.error
import urllib.request

import pytest

from balatro_bot import overlay


@pytest.fixture
def served(tmp_path):
    server = overlay.serve(tmp_path, 0)
    yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers, r.read()


def test_the_page_is_served_at_the_root(served):
    _, base = served
    status, headers, body = get(base + "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b"now.json" in body


def test_the_players_file_is_served_uncached(served):
    folder, base = served
    (folder / "now.json").write_text(json.dumps({"games": 2}))
    status, headers, body = get(base + "/now.json?t=1")
    assert json.loads(body) == {"games": 2} and headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", ["/status.json", "/stop", "/../status.json"])
def test_missing_or_other_files_are_not_found(served, path):
    _, base = served
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + path)
    assert e.value.code == 404
