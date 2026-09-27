"""A specialist may borrow one Codex OAuth store without cloning its grant."""

import json

from hermes_cli.auth import resolve_codex_runtime_credentials
from hermes_constants import get_hermes_home


def test_shared_codex_store_keeps_profile_home_and_other_auth_separate(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    shared = tmp_path / "shared"
    profile.mkdir()
    shared.mkdir()
    profile_auth = {"version": 1, "providers": {}, "credential_pool": {"anthropic": [{"id": "own"}]}}
    shared_auth = {"version": 1, "providers": {"openai-codex": {
        "tokens": {"access_token": "shared-access", "refresh_token": "shared-refresh"},
        "last_refresh": "2026-09-26T00:00:00Z",
    }}}
    (profile / "auth.json").write_text(json.dumps(profile_auth))
    (shared / "auth.json").write_text(json.dumps(shared_auth))
    (shared / "auth.json").chmod(0o600)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("HERMES_CODEX_AUTH_HOME", str(shared))

    result = resolve_codex_runtime_credentials(refresh_if_expiring=False)

    assert result["api_key"] == "shared-access"
    assert get_hermes_home() == profile
    assert json.loads((profile / "auth.json").read_text()) == profile_auth
    assert json.loads((shared / "auth.json").read_text()) == shared_auth
