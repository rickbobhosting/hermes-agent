"""External selectors are bound to one allowlisted home and one capability."""
from __future__ import annotations

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    dashboard, target, sibling = (tmp_path / name for name in ("dashboard", "target", "sibling"))
    for home in (dashboard, target, sibling):
        home.mkdir()
        (home / "config.yaml").write_text("timezone: UTC\n", encoding="utf-8")
    target_db = SessionDB(db_path=target / "state.db")
    target_db.create_session("target-only", "web")
    target_db.append_message("target-only", "user", "target transcript")
    target_db.close()
    sibling_db = SessionDB(db_path=sibling / "state.db")
    sibling_db.create_session("sibling-only", "web")
    sibling_db.append_message("sibling-only", "user", "sibling secret")
    sibling_db.close()

    monkeypatch.setenv("HERMES_HOME", str(dashboard))

    def set_access(access):
        (dashboard / "config.yaml").write_text(yaml.safe_dump({
            "dashboard": {"fleet_targets": {"target": {"home": str(target), "access": access}}}
        }), encoding="utf-8")

    set_access("inspect")
    with TestClient(app) as client:
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        yield client, target, sibling, set_access


def test_inspect_reads_only_registered_session_and_rejects_unknown(fleet):
    client, target, sibling, set_access = fleet
    from hermes_cli.web_server_profiles import _resolve_profile_dir

    detail = client.get("/api/profiles/fleet/target/sessions/target-only")
    assert detail.status_code == 200, detail.text
    assert detail.json()["profile"] == "fleet:target"
    messages = client.get("/api/profiles/fleet/target/sessions/target-only/messages")
    assert messages.status_code == 200, messages.text
    assert messages.json()["profile"] == "fleet:target"
    assert messages.json()["messages"][0]["content"] == "target transcript"
    assert client.get("/api/profiles/fleet/target/sessions/sibling-only/messages").status_code == 404
    assert client.get("/api/profiles/fleet/sibling/sessions/sibling-only").status_code == 404
    assert client.get("/api/profiles/fleet/target/sessions/target-only/messages?limit=501").status_code == 422
    with pytest.raises(HTTPException) as exc:
        _resolve_profile_dir("fleet:sibling")
    assert exc.value.status_code == 404
    assert not (target / "SOUL.md").exists()


def test_manage_write_and_selector_conflicts(fleet):
    client, target, sibling, set_access = fleet
    url = "/api/profiles/fleet%3Atarget/soul"
    body = {"content": "# Managed target\n"}

    assert client.put(url, json=body).status_code == 403
    assert not (target / "SOUL.md").exists()
    set_access("manage")
    assert client.put(url, json=body).status_code == 200
    assert (target / "SOUL.md").read_text(encoding="utf-8") == body["content"]
    assert not (sibling / "SOUL.md").exists()
    config_write = client.put("/api/config?profile=fleet:target", json={
        "profile": "fleet:target", "config": {"timezone": "Europe/London"}
    })
    assert config_write.status_code == 200, config_write.text
    assert yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))["timezone"] == "Europe/London"

    conflict = client.put(url + "?profile=default", json=body)
    assert conflict.status_code == 400
    assert client.put(url + "?profile=fleet:sibling", json=body).status_code == 400
    assert client.put("/api/config?profile=fleet:target", json={
        "profile": "default", "config": {"timezone": "Europe/London"}
    }).status_code == 400
    assert client.post("/api/gateway/restart?profile=fleet:target").status_code == 403
    assert client.post("/api/profiles/active", json={"name": "fleet:target"}).status_code == 403
    assert client.post("/api/profiles", json={
        "name": "new-local", "clone_from": "fleet:target"
    }).status_code == 403


def test_execute_capability_does_not_enable_host_operations(fleet):
    client, target, sibling, set_access = fleet
    from hermes_cli.dashboard_fleet_access import require_fleet_access

    with pytest.raises(HTTPException) as exc:
        require_fleet_access("fleet:target", "execute")
    assert exc.value.status_code == 403
    set_access("execute")
    assert require_fleet_access("fleet:target", "execute").home == target
    assert client.post("/api/gateway/restart?profile=fleet:target").status_code == 403
    assert client.post("/api/profiles/fleet%3Atarget/open-terminal").status_code == 403


def test_invalid_access_defaults_to_inspect(fleet):
    client, target, sibling, set_access = fleet
    set_access("root")
    assert client.put("/api/profiles/fleet%3Atarget/soul", json={"content": "bad"}).status_code == 403
    assert client.get("/api/profiles/fleet/target/sessions/target-only").status_code == 200
