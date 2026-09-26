"""Authorization boundary for explicit dashboard fleet selectors."""
from __future__ import annotations

import json
from urllib.parse import unquote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from hermes_cli.dashboard_fleet import is_fleet_selector, resolve_fleet_target

_RANK = {"inspect": 0, "manage": 1, "execute": 2}
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_fleet_access(profile: str | None, required: str):
    """Resolve an allowlisted selector and require its configured capability."""
    if required not in _RANK:
        raise ValueError(f"Unknown fleet capability: {required}")
    if not is_fleet_selector(profile):
        return None
    target = resolve_fleet_target(profile)
    if target is None:
        raise HTTPException(404, f"Fleet target '{profile}' is not registered.")
    if _RANK[target.access] < _RANK[required]:
        raise HTTPException(403, f"Fleet target '{profile}' requires {required} access.")
    return target


def _selectors(request: Request, body: object) -> list[str]:
    candidates: list[object] = request.query_params.getlist("profile")
    if isinstance(body, dict):
        candidates.append(body.get("profile"))
        # Profile creation can carry a source selector even though its target
        # is a new local name. That host lifecycle route is not fleet-enabled.
        if request.url.path == "/api/profiles":
            candidates.extend(value for value in (body.get("name"), body.get("clone_from"))
                              if is_fleet_selector(value))
        if request.url.path == "/api/profiles/active":
            candidates.append(body.get("name"))
    parts = request.url.path.split("/")
    if len(parts) > 3 and parts[1:3] == ["api", "profiles"] and parts[3] not in {
        "active", "clone", "distributions", "sessions", "fleet", "projects"
    }:
        candidates.append(unquote(parts[3]))
    values = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip() and candidate.strip() not in values:
            values.append(candidate.strip())
    return values


def _required_access(request: Request) -> str | None:
    """Only routes whose implementation scopes to the target home are enabled."""
    path, method = request.url.path, request.method.upper()
    parts = path.split("/")
    if method == "PUT" and len(parts) == 5 and parts[1:3] == ["api", "profiles"]:
        if parts[4] in {"soul", "description", "model"}:
            return "manage"
    if method == "PUT" and path in {"/api/config", "/api/config/raw"}:
        return "manage"
    return None


async def dashboard_fleet_access_middleware(request: Request, call_next):
    if request.method.upper() in _SAFE_METHODS:
        return await call_next(request)
    try:
        raw = await request.body()
        body = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        body = None
    selectors = _selectors(request, body)
    if len(selectors) > 1:
        return JSONResponse(status_code=400, content={"detail": "Conflicting profile selectors are not allowed."})
    selector = selectors[0] if selectors else None
    if is_fleet_selector(selector):
        parts = request.url.path.split("/")
        if (request.method.upper() == "DELETE" and len(parts) == 5
                and parts[1:4] == ["api", "pty", "sessions"]
                and body is None and request.query_params.getlist("profile") == [selector]):
            # The route binds this cleanup-only stop to an existing opaque
            # task ID and its original selector. A revoked registration must
            # not prevent termination of its already-running child.
            return await call_next(request)
        try:
            required = _required_access(request)
            if required is None:
                raise HTTPException(403, "This operation is unavailable for external fleet targets.")
            require_fleet_access(selector, required)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)
