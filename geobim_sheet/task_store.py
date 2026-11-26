"""Persistent JSON task store for Revision Review Agent state and pending confirmations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Thread-safe JSON file-backed store for agent tasks."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir) / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _path(self, task_id: str) -> Path:
        safe = "".join(ch for ch in task_id if ch.isalnum() or ch in ("-", "_"))
        if not safe:
            raise ValueError("Invalid task_id")
        return self.root / f"{safe}.json"

    def create(
        self,
        *,
        instruction: str,
        mode: str,
        provider: str,
        model: str,
        scenario: str | None = None,
        input_files: dict | None = None,
    ) -> dict:
        task_id = f"task-{uuid4().hex[:10]}"
        now = _utc_now()
        task = {
            "task_id": task_id,
            "created_at": now,
            "updated_at": now,
            "status": "running",
            "mode": mode,
            "provider": provider,
            "model": model,
            "scenario": scenario,
            "input_files": input_files or {},
            "project_id": None,
            "instruction": instruction,
            "messages": [],
            "tool_calls": [],
            "pending_question": None,
            "selected_asset_tag": None,
            "selected_asset_guid": None,
            "confirmed_matches": [],
            "missing_info": [],
            "artifacts": [],
            "issues_preview": [],
            "final_summary": "",
            "error": None,
        }
        self.save(task)
        return task

    def save(self, task: dict) -> dict:
        task["updated_at"] = _utc_now()
        target = self._path(task["task_id"])
        tmp = target.with_suffix(".json.tmp")
        payload = json.dumps(task, ensure_ascii=False, indent=2)
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
        return task

    def get(self, task_id: str) -> dict | None:
        target = self._path(task_id)
        with self._lock:
            if not target.is_file():
                return None
            return json.loads(target.read_text(encoding="utf-8"))

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            results = []
            for path in files[:limit]:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    results.append(
                        {
                            "task_id": data["task_id"],
                            "status": data["status"],
                            "scenario": data.get("scenario"),
                            "instruction": data.get("instruction", "")[:120],
                            "updated_at": data.get("updated_at"),
                            "artifact_count": len(data.get("artifacts", [])),
                        }
                    )
                except Exception:
                    continue
            return results
