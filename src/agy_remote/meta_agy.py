"""Meta-AGY V2 job API client.

`agy-remote` acts as a thin control and observation plane over `meta-AGY`.
It does not manage worker processes directly; instead it consumes the asynchronous
job API exposed by meta-AGY.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import AgentRecord

logger = logging.getLogger("agy_remote.meta_agy")


class MetaAgyError(Exception):
    """Raised when communication with meta-AGY fails."""


def normalize_job_status(raw_status: str | None) -> str:
    """Normalize vendor/worker status strings into standard agent lifecycle states."""
    if not raw_status:
        return "running"
    s = str(raw_status).strip().lower()
    if s in ("running", "in_progress", "started", "pending", "executing", "queued"):
        return "running"
    if s in ("needs_attention", "waiting_for_input", "attention", "blocked", "approval", "review"):
        return "needs_attention"
    if s in ("completed", "done", "finished", "success", "succeeded"):
        return "completed"
    if s in ("failed", "error", "errored"):
        return "failed"
    if s in ("cancelled", "canceled", "stopped", "aborted"):
        return "cancelled"
    return s


def normalize_meta_job(data: dict[str, Any]) -> AgentRecord:
    """Convert raw meta-AGY job dictionary into a typed AgentRecord."""
    agent_id = str(data.get("agent_id") or data.get("id") or data.get("job_id") or "")
    backend = "meta-agy"
    provider = str(data.get("provider") or data.get("worker") or "gemini").lower()
    model = data.get("model")
    project = data.get("project") or data.get("repo")
    workspace = data.get("workspace") or data.get("worktree")
    task = data.get("current_task") or data.get("task") or data.get("prompt")
    status = normalize_job_status(data.get("status"))
    started_at = data.get("started_at") or data.get("created_at")
    last_activity = data.get("last_activity") or data.get("updated_at") or started_at

    result = data.get("result")
    if isinstance(result, dict):
        result = result.get("summary") or str(result)

    files_changed = data.get("files_changed") or []
    if isinstance(files_changed, str):
        files_changed = [files_changed]

    tests_run = data.get("tests_run")
    commit = data.get("commit")
    remaining_issues = data.get("remaining_issues") or []
    if isinstance(remaining_issues, str):
        remaining_issues = [remaining_issues]

    output_offset = int(data.get("output_offset", 0))

    return AgentRecord(
        agent_id=agent_id,
        backend=backend,
        provider=provider,
        model=model,
        project=project,
        workspace=workspace,
        current_task=task,
        status=status,
        started_at=started_at,
        last_activity=last_activity,
        result=result,
        files_changed=files_changed,
        tests_run=tests_run,
        commit=commit,
        remaining_issues=remaining_issues,
        output_offset=output_offset,
    )


class MetaAgyClient:
    """HTTP client for the meta-AGY V2 asynchronous job API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        token: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._external_client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Auth-Token"] = self.token
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = self._headers()
        try:
            if self._external_client:
                return await self._external_client.request(
                    method, url, json=json_data, params=params, headers=headers, timeout=self.timeout
                )
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                return await client.request(method, url, json=json_data, params=params, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.debug("meta-AGY connection error on %s %s: %s", method, url, exc)
            raise MetaAgyError(f"Could not connect to meta-AGY at {self.base_url}") from exc
        except Exception as exc:
            logger.debug("meta-AGY request error on %s %s: %s", method, url, exc)
            raise MetaAgyError(f"meta-AGY request failed: {exc}") from exc

    async def list_jobs(self) -> list[AgentRecord]:
        """Fetch list of all worker jobs from meta-AGY."""
        try:
            res = await self._request("GET", "/api/v2/jobs")
            if res.status_code == 404:
                res = await self._request("GET", "/api/jobs")
            if res.status_code != 200:
                logger.debug("meta-AGY returned status %d for list_jobs", res.status_code)
                return []
            data = res.json()
            items = data if isinstance(data, list) else data.get("jobs", data.get("items", []))
            return [normalize_meta_job(item) for item in items if isinstance(item, dict)]
        except MetaAgyError:
            return []
        except Exception as exc:
            logger.debug("Failed parsing meta-AGY jobs response: %s", exc)
            return []

    async def get_job(self, job_id: str) -> AgentRecord | None:
        """Fetch details of a specific job by ID."""
        try:
            res = await self._request("GET", f"/api/v2/jobs/{job_id}")
            if res.status_code == 404:
                res = await self._request("GET", f"/api/jobs/{job_id}")
            if res.status_code == 404:
                return None
            if res.status_code != 200:
                raise MetaAgyError(f"meta-AGY returned {res.status_code} for job {job_id}")
            data = res.json()
            return normalize_meta_job(data if isinstance(data, dict) else {})
        except MetaAgyError:
            raise
        except Exception as exc:
            logger.debug("Failed reading job %s from meta-AGY: %s", job_id, exc)
            return None

    async def submit_job(
        self,
        project: str,
        task: str,
        provider: str = "gemini",
        model: str | None = None,
        context: str | None = None,
    ) -> AgentRecord:
        """Submit a new job to meta-AGY."""
        payload: dict[str, Any] = {
            "project": project,
            "task": task,
            "provider": provider,
        }
        if model:
            payload["model"] = model
        if context:
            payload["context"] = context

        res = await self._request("POST", "/api/v2/jobs", json_data=payload)
        if res.status_code == 404:
            res = await self._request("POST", "/api/jobs", json_data=payload)

        if res.status_code not in (200, 201, 202):
            raise MetaAgyError(f"Failed to submit job to meta-AGY: status {res.status_code} - {res.text}")

        data = res.json()
        if not isinstance(data, dict):
            raise MetaAgyError("Unexpected response shape from meta-AGY job submission")

        # In case response returns nested job data
        job_data = data.get("job", data)
        return normalize_meta_job(job_data)

    async def get_output(self, job_id: str, offset: int = 0) -> tuple[str, int]:
        """Fetch incremental output for a job starting at byte/line offset.

        Returns (content, next_offset).
        """
        try:
            res = await self._request("GET", f"/api/v2/jobs/{job_id}/output", params={"offset": offset})
            if res.status_code == 404:
                res = await self._request("GET", f"/api/jobs/{job_id}/output", params={"offset": offset})
            if res.status_code != 200:
                return ("", offset)

            # Check if response is JSON or plain text
            content_type = res.headers.get("content-type", "")
            if "application/json" in content_type:
                data = res.json()
                if isinstance(data, dict):
                    content = str(data.get("content") or data.get("output") or data.get("delta") or "")
                    next_offset = int(data.get("next_offset") or data.get("offset") or (offset + len(content.encode())))
                    return (content, next_offset)
            text = res.text
            return (text, offset + len(text.encode("utf-8")))
        except Exception as exc:
            logger.debug("Failed getting output for job %s: %s", job_id, exc)
            return ("", offset)

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel an in-flight job in meta-AGY."""
        try:
            res = await self._request("POST", f"/api/v2/jobs/{job_id}/cancel")
            if res.status_code == 404:
                res = await self._request("POST", f"/api/jobs/{job_id}/cancel")
            return res.status_code in (200, 202, 204)
        except Exception as exc:
            logger.debug("Failed cancelling job %s: %s", job_id, exc)
            return False

    async def retry_job(self, job_id: str) -> AgentRecord:
        """Retry or re-execute a job."""
        try:
            res = await self._request("POST", f"/api/v2/jobs/{job_id}/retry")
            if res.status_code in (200, 201, 202):
                data = res.json()
                return normalize_meta_job(data.get("job", data))
        except Exception:
            pass

        # Fallback: retrieve job spec and submit new job
        existing = await self.get_job(job_id)
        if not existing:
            raise MetaAgyError(f"Job {job_id} not found to retry")
        return await self.submit_job(
            project=existing.project or "project",
            task=existing.current_task or "Retry previous task",
            provider=existing.provider,
            model=existing.model,
            context=f"Retry of job {job_id}. Prior result: {existing.result or 'None'}",
        )
