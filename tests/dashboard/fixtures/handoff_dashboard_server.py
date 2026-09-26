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
    parser.add_argument("--profile", default="acceptance")
    args = parser.parse_args()
    home = args.home.resolve(strict=True)
    assert os.environ.get("HERMES_HOME") == str(home)
    assert os.environ.get("HERMES_TEST_ISOLATION") == str(home)
    assert (home / "profiles" / args.profile).is_dir()

    from hermes_cli import web_server, web_server_chat
    from hermes_cli.pty_session import PtySessionRegistry

    root = Path(__file__).resolve().parents[3]
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
    registry = PtySessionRegistry(max_sessions=16, buffer_cap=1024 * 1024, read_timeout=0.2)
    web_server_chat.PTY_REGISTRY = registry
    web_server.PTY_REGISTRY = registry
    web_server._SESSION_TOKEN = secrets.token_urlsafe(32)
    web_server._GATEWAY_HEALTH_URL = None
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = args.port
    web_server.app.state.auth_required = False
    web_server.app.state.trusted_public_hosts = frozenset()
    uvicorn.run(web_server.app, host="127.0.0.1", port=args.port, log_level="error", lifespan="on")


if __name__ == "__main__":
    main()
