"""Unit and integration tests for Meta-AGY client, backend, and unified multi-agent control plane."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.meta_agy import MetaAgyClient, MetaAgyError, normalize_job_status, normalize_meta_job
from agy_remote.models import SessionRecord
from agy_remote.server import create_app
from agy_remote.session_manager import SessionManager


class FakePushManager:
    """Mock push manager to verify notifications."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str, dict[str, Any] | None]] = []

    def send_notification(self, title: str, body: str, data: dict[str, Any] | None = None) -> None:
        self.notifications.append((title, body, data))


def fake_meta_agy_transport(jobs_db: dict[str, dict[str, Any]], outputs_db: dict[str, str]):
    """Creates an httpx.MockTransport simulating meta-AGY V2 job API."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        method = request.method

        if path in ("/api/v2/jobs", "/api/jobs"):
            if method == "GET":
                return httpx.Response(200, json={"jobs": list(jobs_db.values())})
            if method == "POST":
                data = json.loads(request.content.decode("utf-8"))
                job_id = f"job-{len(jobs_db) + 1}"
                job = {
                    "id": job_id,
                    "provider": data.get("provider", "gemini"),
                    "project": data.get("project"),
                    "task": data.get("task"),
                    "model": data.get("model"),
                    "context": data.get("context"),
                    "status": "running",
                    "started_at": "2026-09-01T10:00:00Z",
                    "last_activity": "2026-09-01T10:00:00Z",
                }
                jobs_db[job_id] = job
                outputs_db[job_id] = f"Initialized {data.get('provider')} worker on {data.get('project')}\n"
                return httpx.Response(201, json={"job": job})

        if path.startswith("/api/v2/jobs/") or path.startswith("/api/jobs/"):
            rest = path.replace("/api/v2/jobs/", "").replace("/api/jobs/", "")
            parts = rest.split("/")
            job_id = parts[0]

            if len(parts) == 1:
                if job_id not in jobs_db:
                    return httpx.Response(404, json={"detail": "Job not found"})
                return httpx.Response(200, json=jobs_db[job_id])

            if len(parts) == 2 and parts[1] == "output":
                if job_id not in jobs_db:
                    return httpx.Response(404, json={"detail": "Job not found"})
                raw_offset = url.params.get("offset", "0")
                offset = int(raw_offset)
                full_text = outputs_db.get(job_id, "")
                encoded = full_text.encode("utf-8")
                delta = encoded[offset:].decode("utf-8", errors="replace")
                return httpx.Response(
                    200,
                    json={
                        "agent_id": job_id,
                        "offset": offset,
                        "next_offset": len(encoded),
                        "content": delta,
                    },
                )

            if len(parts) == 2 and parts[1] == "cancel":
                if job_id not in jobs_db:
                    return httpx.Response(404, json={"detail": "Job not found"})
                jobs_db[job_id]["status"] = "cancelled"
                return httpx.Response(200, json={"status": "ok", "job_id": job_id})

            if len(parts) == 2 and parts[1] == "retry":
                if job_id not in jobs_db:
                    return httpx.Response(404, json={"detail": "Job not found"})
                old = jobs_db[job_id]
                new_id = f"{job_id}-retry"
                new_job = dict(old)
                new_job["id"] = new_id
                new_job["status"] = "running"
                jobs_db[new_id] = new_job
                outputs_db[new_id] = f"Retrying {new_job.get('task')}\n"
                return httpx.Response(201, json={"job": new_job})

        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Status and normalization unit tests
# ---------------------------------------------------------------------------


def test_normalize_job_status():
    assert normalize_job_status("in_progress") == "running"
    assert normalize_job_status("queued") == "running"
    assert normalize_job_status("blocked") == "needs_attention"
    assert normalize_job_status("waiting_for_input") == "needs_attention"
    assert normalize_job_status("done") == "completed"
    assert normalize_job_status("succeeded") == "completed"
    assert normalize_job_status("errored") == "failed"
    assert normalize_job_status("canceled") == "cancelled"


def test_normalize_meta_job():
    raw = {
        "job_id": "gemini-123",
        "worker": "Gemini",
        "model": "gemini-2.5-flash",
        "repo": "transposer",
        "prompt": "Fix SQLite persistence",
        "status": "done",
        "result": {"summary": "Added durable write-ahead log"},
        "files_changed": ["src/db.py", "tests/test_db.py"],
        "commit": "abc1234",
        "remaining_issues": ["Check connection pooling"],
    }
    rec = normalize_meta_job(raw)
    assert rec.agent_id == "gemini-123"
    assert rec.backend == "meta-agy"
    assert rec.provider == "gemini"
    assert rec.model == "gemini-2.5-flash"
    assert rec.project == "transposer"
    assert rec.current_task == "Fix SQLite persistence"
    assert rec.status == "completed"
    assert rec.result == "Added durable write-ahead log"
    assert "src/db.py" in rec.files_changed
    assert rec.commit == "abc1234"


# ---------------------------------------------------------------------------
# MetaAgyClient unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_agy_client_operations():
    jobs_db = {}
    outputs_db = {}
    transport = fake_meta_agy_transport(jobs_db, outputs_db)
    client = httpx.AsyncClient(transport=transport)
    meta_client = MetaAgyClient(base_url="http://fake-meta-agy", client=client)

    # 1. Submit job
    job = await meta_client.submit_job(
        project="transposer",
        task="Implement persistence",
        provider="gemini",
        model="flash",
        context="Inspect arch first",
    )
    assert job.agent_id == "job-1"
    assert job.status == "running"
    assert job.project == "transposer"

    # 2. List jobs
    jobs = await meta_client.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].agent_id == "job-1"

    # 3. Get job
    fetched = await meta_client.get_job("job-1")
    assert fetched is not None
    assert fetched.agent_id == "job-1"

    # 4. Get incremental output
    content, next_offset = await meta_client.get_output("job-1", offset=0)
    assert "Initialized gemini worker" in content
    assert next_offset > 0

    # No new output at next_offset
    content_next, offset_same = await meta_client.get_output("job-1", offset=next_offset)
    assert content_next == ""
    assert offset_same == next_offset

    # 5. Append output and retrieve incrementally
    outputs_db["job-1"] += "Compiling storage module\n"
    delta, final_offset = await meta_client.get_output("job-1", offset=next_offset)
    assert delta == "Compiling storage module\n"
    assert final_offset > next_offset

    # 6. Cancel job
    cancelled = await meta_client.cancel_job("job-1")
    assert cancelled is True
    assert jobs_db["job-1"]["status"] == "cancelled"

    # 7. Retry job
    retried = await meta_client.retry_job("job-1")
    assert retried.agent_id == "job-1-retry"
    assert retried.status == "running"


@pytest.mark.asyncio
async def test_meta_agy_client_connection_failure():
    # Transport that always raises connection error
    def failing_handler(request: httpx.Request):
        raise httpx.ConnectError("Connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    meta_client = MetaAgyClient(base_url="http://dead-host", client=client)

    # List jobs fails gracefully and returns empty list
    jobs = await meta_client.list_jobs()
    assert jobs == []

    # Submit job raises MetaAgyError
    with pytest.raises(MetaAgyError, match="Could not connect"):
        await meta_client.submit_job(project="proj", task="task")


@pytest.mark.asyncio
async def test_meta_agy_client_malformed_response():
    def malformed_handler(request: httpx.Request):
        return httpx.Response(200, content=b"not-json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(malformed_handler))
    meta_client = MetaAgyClient(base_url="http://bad-response", client=client)

    # Should not crash, returns empty list
    jobs = await meta_client.list_jobs()
    assert jobs == []


# ---------------------------------------------------------------------------
# SessionManager multi-agent integration & state tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_manager_multi_agent_coexistence(tmp_path: Path):
    jobs_db = {
        "job-1": {
            "id": "job-1",
            "provider": "gemini",
            "project": "transposer",
            "task": "Implement persistence",
            "status": "running",
            "started_at": "2026-09-01T10:00:00Z",
        },
        "job-2": {
            "id": "job-2",
            "provider": "claude",
            "project": "transposer",
            "task": "Investigate auth",
            "status": "needs_attention",
            "started_at": "2026-09-01T10:05:00Z",
        },
        "job-3": {
            "id": "job-3",
            "provider": "gemini",
            "project": "podcast",
            "task": "Loudness tests",
            "status": "completed",
            "started_at": "2026-09-01T09:00:00Z",
        },
    }
    outputs_db = {"job-1": "Working...", "job-2": "Attention required...", "job-3": "Done"}
    transport = fake_meta_agy_transport(jobs_db, outputs_db)
    mock_client = MetaAgyClient(base_url="http://mock-meta", client=httpx.AsyncClient(transport=transport))

    push_mgr = FakePushManager()
    cfg = RemoteConfig(brain_dir=tmp_path, enable_auth=False, e2ee_enabled=False)
    mgr = SessionManager(config=cfg, push_manager=push_mgr, meta_agy_client=mock_client)

    # Register an Antigravity native session
    mgr.register_session(
        SessionRecord(
            id="native-agy-1",
            tmux_name="agy-session-1",
            conversation_id="conv-123",
            workdir="/Users/dst/Projects/my-app",
            busy=True,
        )
    )

    agents = await mgr.list_agents()
    assert len(agents) == 4

    # Priority sort verification:
    # 0: needs_attention (Claude job-2)
    # 1: running (Antigravity conv-123 & Gemini job-1)
    # 2: completed (Gemini job-3)
    assert agents[0].agent_id == "job-2"
    assert agents[0].status == "needs_attention"
    assert agents[0].provider == "claude"

    running_ids = {agents[1].agent_id, agents[2].agent_id}
    assert "job-1" in running_ids
    assert "conv-123" in running_ids

    assert agents[3].agent_id == "job-3"
    assert agents[3].status == "completed"

    # Reconnect snapshot test: _init_data contains all 4 agents
    init_data = mgr._init_data()
    agent_records_init = init_data["data"]["agents"]
    assert len(agent_records_init) == 4


@pytest.mark.asyncio
async def test_session_manager_notifications_on_state_transition(tmp_path: Path):
    jobs_db = {
        "job-1": {
            "id": "job-1",
            "provider": "gemini",
            "project": "transposer",
            "task": "Persistent storage",
            "status": "running",
            "started_at": "2026-09-01T10:00:00Z",
        }
    }
    outputs_db = {"job-1": "Running"}
    transport = fake_meta_agy_transport(jobs_db, outputs_db)
    mock_client = MetaAgyClient(base_url="http://mock-meta", client=httpx.AsyncClient(transport=transport))

    push_mgr = FakePushManager()
    cfg = RemoteConfig(brain_dir=tmp_path, enable_auth=False, e2ee_enabled=False)
    mgr = SessionManager(config=cfg, push_manager=push_mgr, meta_agy_client=mock_client)

    # First poll seeds cache (no transition notification)
    await mgr.poll_meta_agy()
    assert len(push_mgr.notifications) == 0

    # Job transitions to completed
    jobs_db["job-1"]["status"] = "completed"
    await mgr.poll_meta_agy()
    assert len(push_mgr.notifications) == 1
    title, body, data = push_mgr.notifications[0]
    assert "Completed" in title
    assert data["status"] == "completed"
    assert data["agent_id"] == "job-1"

    # Job transitions to failed
    jobs_db["job-1"]["status"] = "failed"
    await mgr.poll_meta_agy()
    assert len(push_mgr.notifications) == 2
    assert "Failed" in push_mgr.notifications[1][0]

    # Job transitions to needs_attention
    jobs_db["job-1"]["status"] = "needs_attention"
    await mgr.poll_meta_agy()
    assert len(push_mgr.notifications) == 3
    assert "Needs Attention" in push_mgr.notifications[2][0]


# ---------------------------------------------------------------------------
# Server REST API and WebSocket integration tests
# ---------------------------------------------------------------------------


def test_server_agents_rest_api(tmp_path: Path):
    jobs_db = {
        "job-1": {
            "id": "job-1",
            "provider": "gemini",
            "project": "transposer",
            "task": "Implement persistence",
            "status": "running",
            "started_at": "2026-09-01T10:00:00Z",
        }
    }
    outputs_db = {"job-1": "Starting persistence worker...\n"}
    transport = fake_meta_agy_transport(jobs_db, outputs_db)
    mock_client = MetaAgyClient(base_url="http://mock-meta", client=httpx.AsyncClient(transport=transport))

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok123", enable_auth=True, e2ee_enabled=False)
    mgr = SessionManager(config=cfg, meta_agy_client=mock_client)
    app = create_app(cfg, session_mgr=mgr)
    client = TestClient(app)

    # 1. Auth required
    assert client.get("/api/agents").status_code == 401

    # 2. List agents
    resp = client.get("/api/agents?token=tok123")
    assert resp.status_code == 200
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "job-1"

    # 3. Get single agent
    resp = client.get("/api/agents/job-1?token=tok123")
    assert resp.status_code == 200
    assert resp.json()["provider"] == "gemini"

    # 4. Get incremental output
    resp = client.get("/api/agents/job-1/output?token=tok123&offset=0")
    assert resp.status_code == 200
    out = resp.json()
    assert "Starting persistence" in out["content"]
    assert out["next_offset"] > 0

    # 5. Submit new job
    post_payload = {
        "project": "podcast",
        "task": "Check loudness levels",
        "provider": "claude",
        "model": "claude-3-7-sonnet",
        "context": "Prior analysis identified -14 LUFS discrepancy",
    }
    resp = client.post("/api/agents/jobs?token=tok123", json=post_payload)
    assert resp.status_code == 201
    created = resp.json()
    assert created["agent_id"] == "job-2"
    assert created["provider"] == "claude"
    assert created["project"] == "podcast"

    # 6. Cancel agent
    resp = client.post("/api/agents/job-2/cancel?token=tok123")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 7. Retry agent
    resp = client.post("/api/agents/job-1/retry?token=tok123")
    assert resp.status_code == 200
    retried = resp.json()
    assert retried["agent_id"] == "job-1-retry"


def test_server_agents_websocket_flow(tmp_path: Path):
    jobs_db = {
        "job-1": {
            "id": "job-1",
            "provider": "gemini",
            "project": "transposer",
            "task": "Implement persistence",
            "status": "running",
            "started_at": "2026-09-01T10:00:00Z",
        }
    }
    outputs_db = {"job-1": "Live log line 1\n"}
    transport = fake_meta_agy_transport(jobs_db, outputs_db)
    mock_client = MetaAgyClient(base_url="http://mock-meta", client=httpx.AsyncClient(transport=transport))

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="tok123", enable_auth=True, e2ee_enabled=False)
    mgr = SessionManager(config=cfg, meta_agy_client=mock_client)
    app = create_app(cfg, session_mgr=mgr)

    with TestClient(app) as client, client.websocket_connect("/ws?token=tok123") as ws:
        # Initial event must carry agents
        init_frame = ws.receive_json()
        assert init_frame["event"] == "init"
        assert "agents" in init_frame["data"]
        assert len(init_frame["data"]["agents"]) == 1

        # Request agents via action
        ws.send_json({"action": "get_agents"})
        agents_frame = ws.receive_json()
        if agents_frame["event"] == "peers":
            agents_frame = ws.receive_json()
        assert agents_frame["event"] == "agent_updated"
        assert len(agents_frame["data"]["agents"]) == 1

        # Request live output
        ws.send_json({"action": "get_agent_output", "data": {"agent_id": "job-1", "offset": 0}})
        output_frame = ws.receive_json()
        assert output_frame["event"] == "agent_output"
        assert output_frame["data"]["content"] == "Live log line 1\n"
