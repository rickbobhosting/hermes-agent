"""Explicit external dashboard homes remain visible and strictly read-only."""
from __future__ import annotations

import yaml
from fastapi.testclient import TestClient


def test_fleet_discovery_and_session_read_never_grant_writes(tmp_path, monkeypatch):
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
    db.close()

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
        assert target["access"] == "inspect"  # configured execute is not granted yet
        assert target["read_only"] is True
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

        # The shared profile resolver remains local-only, including for routes
        # that could mutate SOUL, model, config, or gateway state.
        from fastapi import HTTPException
        try:
            _resolve_profile_dir("fleet:infra")
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("Fleet selector entered generic write resolver")
        assert client.put("/api/profiles/fleet:infra/description", json={"description": "changed"},
                          headers=headers).status_code == 400
    assert "changed" not in (external / "config.yaml").read_text(encoding="utf-8")
