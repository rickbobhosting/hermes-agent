"""Disposable real dashboard process for the Turn 9 proxy/browser rehearsal.

The caller supplies a private HERMES_HOME and HERMES_TEST_ISOLATION. This
process patches only its own PTY child command to a fixed fixture, so no model
or live gateway is contacted.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--basic-auth", action="store_true")
    parser.add_argument("--profile", default="acceptance")
    args = parser.parse_args()
    home = args.home.resolve(strict=True)
    assert os.environ.get("HERMES_HOME") == str(home)
    assert os.environ.get("HERMES_TEST_ISOLATION") == str(home)
    assert (home / "profiles" / args.profile).is_dir()

    from hermes_cli import web_server, web_server_chat
    from hermes_cli.pty_session import PtySessionRegistry

    root = Path(__file__).resolve().parents[3]
    server_root = args.server_root.resolve(strict=True)
    assert Path(web_server.__file__).resolve().is_relative_to(server_root)
    child = Path(__file__).with_name("continuity_tui.py")

    def fixture_argv(*, resume=None, sidecar_url=None, profile=None, active_session_file=None):
        assert profile == args.profile
        profile_home = home / "profiles" / args.profile
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ["HOME"],
            "HERMES_HOME": str(profile_home),
            "HERMES_TUI_RESUME": resume or "fixture-fresh",
            "HERMES_TEST_ISOLATION": str(home),
            "PYTHONPATH": str(root),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        if os.environ.get("LANG"):
            child_env["LANG"] = os.environ["LANG"]
        return [sys.executable, str(child)], str(root), child_env

    web_server_chat._resolve_chat_argv = fixture_argv
    registry = PtySessionRegistry(ttl=30 * 60, max_sessions=16, buffer_cap=1024 * 1024, read_timeout=0.2)
    web_server_chat.PTY_REGISTRY = registry
    web_server.PTY_REGISTRY = registry

    def candidate_fixture_pty():
        from fastapi import HTTPException

        matches = [session for session in registry._sessions.values()
                   if session.metadata.get("resume") == "candidate-saved"]
        if len(matches) != 1:
            raise HTTPException(409, "expected one candidate fixture PTY")
        return matches[0]

    @web_server.app.get("/__fixture/pty-identity")
    async def fixture_pty_identity():
        session = candidate_fixture_pty()
        return {"id": session.id, "pid": session.bridge.pid, "attached": session.attached}

    @web_server.app.post("/__fixture/drop-pty")
    async def drop_fixture_pty():
        from fastapi import HTTPException

        session = candidate_fixture_pty()
        if not session.attached:
            raise HTTPException(409, "candidate fixture PTY is detached")
        await session._attachment.ws.close(code=1001, reason="fixture_transport_drop")
        return {"id": session.id, "pid": session.bridge.pid}

    # The production SPA catch-all is already registered; fixture-only routes
    # must precede it or a 200 HTML page masks a failed drop assertion.
    fixture_routes = [route for route in web_server.app.router.routes
                      if route.path.startswith("/__fixture/")]
    web_server.app.router.routes[:] = fixture_routes + [route for route in web_server.app.router.routes
                                                        if route not in fixture_routes]

    web_server._SESSION_TOKEN = secrets.token_urlsafe(32)
    web_server._GATEWAY_HEALTH_URL = None
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = args.port
    if args.basic_auth:
        from hermes_cli.dashboard_auth.registry import register_global_provider
        from plugins.dashboard_auth.basic import BasicAuthProvider, hash_password

        register_global_provider(BasicAuthProvider(
            username="fixture", password_hash=hash_password("fixture-password"),
            secret=b"disposable-dashboard-handoff-secret-32",
        ))
    web_server.app.state.auth_required = args.basic_auth
    web_server.app.state.trusted_public_hosts = frozenset()
    uvicorn.run(web_server.app, host="127.0.0.1", port=args.port, log_level="error", lifespan="on")


if __name__ == "__main__":
    main()
