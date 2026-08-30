"""The mailbox control surface: the PWA's mute toggle and traffic readout.

Delivery and the guard itself are covered by `test_mailbox_loop.py`; these
tests pin the REST contract the PWA calls: who may see pair state, and how a
mute/unmute round-trips.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.server import create_app

TOKEN = "secret123"


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token=TOKEN, enable_auth=True)
    app = create_app(cfg)
    return TestClient(app), app.state.session_manager


# -- GET /api/mailbox -----------------------------------------------------------


def test_mailbox_status_is_empty_until_traffic_exists(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.get(f"/api/mailbox?token={TOKEN}")
    assert resp.status_code == 200
    assert resp.json() == {"pairs": []}


def test_mailbox_status_requires_a_token(tmp_path: Path):
    client, _ = _client(tmp_path)
    assert client.get("/api/mailbox").status_code == 401


def test_mailbox_status_reports_counts_loop_and_mute(tmp_path: Path):
    client, mgr = _client(tmp_path)
    # Seed the guard the way delivery would: two exchanges, then a mute on a
    # pair that has not talked yet.
    mgr.loop_guard.commit("agy-a", "agy-b")
    mgr.loop_guard.commit("agy-a", "agy-b")
    mgr.mute_mailbox_pair("agy-b", "agy-c")

    pairs = {tuple(sorted((p["a"], p["b"]))): p for p in client.get(f"/api/mailbox?token={TOKEN}").json()["pairs"]}
    assert pairs[("agy-a", "agy-b")]["count"] == 2
    assert pairs[("agy-a", "agy-b")]["looping"] is False
    assert pairs[("agy-a", "agy-b")]["muted"] is False
    assert pairs[("agy-b", "agy-c")]["count"] == 0
    assert pairs[("agy-b", "agy-c")]["muted"] is True


# -- POST / DELETE /api/mailbox/mute ---------------------------------------------


def test_mute_round_trip(tmp_path: Path):
    client, _ = _client(tmp_path)
    body = {"a": "agy-a", "b": "agy-b"}

    resp = client.post(f"/api/mailbox/mute?token={TOKEN}", json=body)
    assert resp.status_code == 200
    assert resp.json()["pair"]["muted"] is True

    # the mute is undirected: the order of the names does not matter
    pairs = {tuple(sorted((p["a"], p["b"]))): p for p in client.get(f"/api/mailbox?token={TOKEN}").json()["pairs"]}
    assert pairs[("agy-a", "agy-b")]["muted"] is True

    resp = client.request("DELETE", f"/api/mailbox/mute?token={TOKEN}", json=body)
    assert resp.status_code == 200
    assert resp.json()["pair"]["muted"] is False
    pairs = {tuple(sorted((p["a"], p["b"]))): p for p in client.get(f"/api/mailbox?token={TOKEN}").json()["pairs"]}
    assert pairs[("agy-a", "agy-b")]["muted"] is False


def test_mute_requires_a_token(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.post("/api/mailbox/mute", json={"a": "agy-a", "b": "agy-b"})
    assert resp.status_code == 401


def test_mute_rejects_names_outside_the_mailbox_alphabet(tmp_path: Path):
    client, mgr = _client(tmp_path)
    for bad in ("../etc/passwd", "a/b", "a b", "a.b", ""):
        resp = client.post(f"/api/mailbox/mute?token={TOKEN}", json={"a": bad, "b": "agy-b"})
        assert resp.status_code == 400, bad
    # and the guard was never touched
    assert mgr.agent_traffic() == []


def test_mute_rejects_a_session_talking_to_itself(tmp_path: Path):
    client, mgr = _client(tmp_path)
    resp = client.post(f"/api/mailbox/mute?token={TOKEN}", json={"a": "agy-a", "b": "agy-a"})
    assert resp.status_code == 400
    assert mgr.agent_traffic() == []


def test_mute_rejects_a_malformed_body(tmp_path: Path):
    client, _ = _client(tmp_path)
    assert client.post(f"/api/mailbox/mute?token={TOKEN}", json={"a": "agy-a"}).status_code == 422
    assert client.post(f"/api/mailbox/mute?token={TOKEN}", json={}).status_code == 422


def test_unmuting_a_pair_that_was_never_muted_is_a_no_op(tmp_path: Path):
    client, _ = _client(tmp_path)
    resp = client.request("DELETE", f"/api/mailbox/mute?token={TOKEN}", json={"a": "agy-a", "b": "agy-b"})
    assert resp.status_code == 200
    assert resp.json()["pair"]["muted"] is False
