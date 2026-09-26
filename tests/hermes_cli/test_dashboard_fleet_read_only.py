"""Explicit external dashboard homes require scoped capabilities."""
from __future__ import annotations

import yaml
from fastapi.testclient import TestClient


def test_fleet_discovery_and_session_read_respect_configured_access(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from hermes_cli.web_server_profiles import _resolve_profile_dir

    dashboard = tmp_path / "dashboard"
    external = tmp_path / "external" / "infra"
    sibling = tmp_path / "external" / "unregistered"
    for home in (dashboard, external, sibling):
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    (dashboard / "config.yaml").write_text(yaml.safe_dump({
        "dashboard": {"fleet_targets": {
            "infra": {"home": str(external), "label": "Infra", "zone": "infrastructure", "access": "execute"},
            "relative": {"home": "../external/unregistered"},
            "missing": {"home": str(tmp_path / "missing")},
        }}
    }), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(dashboard))

    db = SessionDB(db_path=external / "state.db")
    db.create_session("fleet-session", "web")
    db.append_messages_batch("fleet-session", [
        {"role": "user", "content": "Fleet prompt", "timestamp": 10},
        {"role": "assistant", "content": "Fleet answer", "timestamp": 11},
    ])
    db.close()
    sibling_db = SessionDB(db_path=sibling / "state.db")
    sibling_db.create_session("sibling-session", "web")
    sibling_db.append_messages_batch("sibling-session", [
        {"role": "user", "content": "Sibling secret", "timestamp": 12},
    ])
    sibling_db.close()

    with TestClient(app) as client:
        headers = {_SESSION_HEADER_NAME: _SESSION_TOKEN}
        assert client.get("/api/profiles/fleet/infra/sessions").status_code == 401
        response = client.get("/api/profiles", headers=headers)
        assert response.status_code == 200, response.text
        profiles = {p["name"]: p for p in response.json()["profiles"]}
        target = profiles["fleet:infra"]
        assert target["path"] == str(external)
        assert target["display_name"] == "Infra"
        assert target["zone"] == "infrastructure"
        assert target["external"] is True
        assert target["access"] == "execute"
        assert target["read_only"] is False
        assert "fleet:relative" not in profiles
        assert "fleet:missing" not in profiles
        assert all(str(sibling) != p["path"] for p in profiles.values())

        sessions = client.get("/api/profiles/fleet/infra/sessions", headers=headers)
        assert sessions.status_code == 200, sessions.text
        assert sessions.json()["total"] == 1
        assert sessions.json()["sessions"][0]["id"] == "fleet-session"
        assert sessions.json()["sessions"][0]["profile"] == "fleet:infra"
        assert client.get("/api/profiles/fleet/unregistered/sessions", headers=headers).status_code == 404
        assert client.get("/api/profiles/fleet/infra/sessions?limit=101", headers=headers).status_code == 422

        timeline_url = "/api/profiles/fleet/infra/sessions/fleet-session/timeline"
        assert client.get(timeline_url).status_code == 401
        timeline = client.get(timeline_url, headers=headers)
        assert timeline.status_code == 200, timeline.text
        assert timeline.json()["profile"] == "fleet:infra"
        assert [entry["preview"] for entry in timeline.json()["entries"]] == ["Fleet prompt"]
        assert b"Sibling secret" not in timeline.content
        anchor = timeline.json()["entries"][0]["row_id"]
        around_url = "/api/profiles/fleet/infra/sessions/fleet-session/messages/around"
        around = client.get(around_url, params={"row_id": anchor}, headers=headers)
        assert around.status_code == 200, around.text
        assert [row["content"] for row in around.json()["messages"]] == ["Fleet prompt", "Fleet answer"]
        assert client.get(timeline_url.replace("fleet-session", "sibling-session"), headers=headers).status_code == 404
        descendant_url = "/api/profiles/fleet/infra/sessions/fleet-session/latest-descendant"
        assert client.get(descendant_url).status_code == 401
        descendant = client.get(descendant_url, headers=headers)
        assert descendant.status_code == 200, descendant.text
        assert descendant.json()["session_id"] == "fleet-session"
        assert descendant.json()["path"] == ["fleet-session"]
        assert client.get(descendant_url.replace("fleet-session", "sibling-session"), headers=headers).status_code == 404
        assert client.get(around_url, params={"row_id": anchor + 1}, headers=headers).status_code == 404
        assert client.get(around_url, params={"row_id": anchor, "limit": 121}, headers=headers).status_code == 422

        assert _resolve_profile_dir("fleet:infra") == external
        assert client.post("/api/gateway/restart?profile=fleet:infra", headers=headers).status_code == 403
