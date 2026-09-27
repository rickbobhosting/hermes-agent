"""Project cron jobs can deliver only to their own Matrix room."""

import json

import pytest

from cron import project_delivery_policy, scheduler_delivery
from tools import cronjob_tools


@pytest.fixture
def project_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(project_delivery_policy, "is_project_profile", lambda: True)
    monkeypatch.setattr(cronjob_tools, "is_project_profile", lambda: True)
    monkeypatch.setattr(
        "agent.secret_scope.get_secret",
        lambda key, default="": "!own:example.org" if key == "MATRIX_HOME_ROOM" else default,
    )


def test_project_cron_defaults_to_home_room_and_refuses_other_targets(
    project_room: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        cronjob_tools._JOBLESS_ACTIONS,
        "create",
        lambda args: json.dumps({"deliver": args["deliver"]}),
    )

    assert json.loads(cronjob_tools.cronjob("create", prompt="check", schedule="1h")) == {
        "deliver": "matrix"
    }
    rejected = cronjob_tools.cronjob(
        "create", prompt="check", schedule="1h", deliver="email:other@example.org"
    )
    assert "limited to its own Matrix room" in rejected
    assert "limited to its own Matrix room" in cronjob_tools.cronjob(
        "create", prompt="check", schedule="1h", failure_deliver="teams:someone"
    )
    assert "profile workspace" in cronjob_tools.cronjob(
        "create", prompt="check", schedule="1h", script="send.sh"
    )


def test_scheduler_drops_wrong_room_even_if_job_was_edited_on_disk(
    project_room: None,
) -> None:
    job = {"id": "project-job", "deliver": "matrix:!other:example.org"}
    assert scheduler_delivery._resolve_delivery_targets(job) == []
    assert scheduler_delivery._resolve_delivery_targets(
        {"id": "project-job", "deliver": "local"}
    ) == []
    assert scheduler_delivery._resolve_delivery_targets(
        {"id": "project-job", "deliver": "matrix", "failure_deliver": "email:other@example.org"},
        for_failure=True,
    ) == []
