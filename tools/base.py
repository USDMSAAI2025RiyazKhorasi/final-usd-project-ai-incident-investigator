"""Tool contract (Jira AII-6). Frozen at Gate 1.

Every read-only tool runs in two modes with identical signatures:
  live      -> Prometheus / Loki / deploy log / git
  snapshot  -> files under snapshots/<scenario_id>/ written by the recorder

Tools return a short summary for the agent and store the full raw result, so
evidence can point to it (Provenance.raw_ref / raw_hash).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from agent.state import Provenance

Mode = Literal["live", "snapshot"]


class ToolResult(BaseModel):
    summary: str               # what the agent reads: top patterns, anomalies, change points
    provenance: Provenance     # attach to any Evidence built from this result


class Backend(Protocol):
    """Implemented once for live and once for snapshot mode."""
    def query_metrics(self, promql: str, start: datetime, end: datetime) -> dict: ...
    def search_logs(self, service: str, query: str, start: datetime, end: datetime) -> list[dict]: ...
    def list_deploys(self, service: str | None, since: datetime) -> list[dict]: ...
    def get_commit_diff(self, sha: str) -> dict: ...
    def get_service_graph(self) -> dict: ...


class RawStore:
    """Stores raw tool results so evidence can be re-checked."""
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put(self, tool: str, args: dict, raw: object, mode: Mode,
            data_start: datetime, data_end: datetime, ok: bool = True) -> Provenance:
        text = json.dumps(raw, sort_keys=True, default=str)
        digest = hashlib.sha256(text.encode()).hexdigest()
        path = self.root / f"{tool}-{digest[:16]}.json"
        path.write_text(text)
        return Provenance(tool=tool, args=args, called_at=datetime.now(timezone.utc),
                          data_start=data_start, data_end=data_end, raw_ref=str(path),
                          raw_hash=digest, mode=mode, ok=ok)


# Tool signatures the agents see (implemented in tools/<name>.py):
#   query_metrics(promql: str, start: datetime, end: datetime) -> ToolResult
#   search_logs(service: str, query: str, start: datetime, end: datetime) -> ToolResult
#   list_deploys(service: str | None, since: datetime) -> ToolResult
#   get_commit_diff(sha: str) -> ToolResult
#   get_service_graph() -> ToolResult
#   search_runbooks(query: str, k: int = 5) -> ToolResult     # shared by all agents
# Write tools (restart_service, rollback_deploy, scale_service) live in
# agent/remediation/executor.py and are never exposed to agents.
