"""Kanban dashboard routes: read-only task list/detail for the native Kanban board.

Reads directly from ``kanban.db`` (the same SQLite store ``hermes kanban`` reads/writes) in
read-only mode. No mutation endpoints here on purpose: the dashboard view is a visibility tool,
not a second control surface for kanban lifecycle actions (those stay on ``hermes kanban``).
"""

import json
import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from hermes_cli.web_deps import late
from hermes_cli import kanban_db

_require_token = late("_require_token")

router = APIRouter()

_TASK_FIELDS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by", "created_at",
    "started_at", "completed_at", "workspace_kind", "workspace_path", "branch_name", "tenant",
    "result", "consecutive_failures", "worker_pid", "last_failure_error", "last_heartbeat_at",
)


def _connect(board: Optional[str] = None) -> sqlite3.Connection:
    try:
        db = kanban_db.kanban_db_path(board=board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not db.exists():
        raise HTTPException(status_code=404, detail="kanban.db not found")
    # Read-only URI connection: this route must never be able to mutate task state.
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _task_dict(row: sqlite3.Row) -> Dict[str, Any]:
    keys = row.keys()
    return {field: row[field] for field in _TASK_FIELDS if field in keys}


def _event_dict(row: sqlite3.Row) -> Dict[str, Any]:
    payload = row["payload"]
    try:
        payload = json.loads(payload) if payload else None
    except (TypeError, ValueError):
        pass
    return {"kind": row["kind"], "payload": payload, "created_at": row["created_at"], "run_id": row["run_id"]}


@router.get("/api/kanban/tasks")
async def list_kanban_tasks(
    request: Request,
    profile: Optional[str] = None,
    status: Optional[str] = None,
    board: Optional[str] = None,
    limit: int = 100,
):
    """Task list, newest first. Filter by assignee profile and/or status."""
    _require_token(request)
    conn = _connect(board)
    try:
        query = (
            "SELECT " + ", ".join(_TASK_FIELDS) + " FROM tasks WHERE 1=1"
        )
        params: List[Any] = []
        if profile:
            query += " AND assignee = ?"
            params.append(profile)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = conn.execute(query, params).fetchall()
        return {"tasks": [_task_dict(r) for r in rows]}
    finally:
        conn.close()


@router.get("/api/kanban/tasks/{task_id}")
async def get_kanban_task(request: Request, task_id: str, board: Optional[str] = None):
    """One task with its comments and recent events, newest events first."""
    _require_token(request)
    conn = _connect(board)
    try:
        task_row = conn.execute(
            "SELECT " + ", ".join(_TASK_FIELDS) + " FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            raise HTTPException(status_code=404, detail="task not found")
        comments = conn.execute(
            "SELECT author, body, created_at FROM task_comments WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT kind, payload, created_at, run_id FROM task_events "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 100",
            (task_id,),
        ).fetchall()
        return {
            "task": _task_dict(task_row),
            "comments": [dict(c) for c in comments],
            "events": [_event_dict(e) for e in events],
        }
    finally:
        conn.close()
