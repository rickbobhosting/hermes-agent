"""Tests for the webhook adapter's ``cron_job`` route mode.

``cron_job`` routes turn an existing cron job into an event-triggered task:
an inbound webhook event fires the referenced job through the same
claimed-run body a manual ``cronjob(action='run')`` uses, instead of
starting a fresh webhook agent session.

Covers:
- The referenced job is fired via ``execute_job_for_event`` with the
  rendered prompt as transient per-run context
- The normal webhook agent session is NOT started (``handle_message``
  never called)
- HTTP returns 202 Accepted immediately
- Startup validation rejects routes that set both ``cron_job`` and
  ``deliver_only``
- ``execute_job_for_event`` resolves refs and fails cleanly on unknowns
"""

import asyncio
import json
import threading
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _drain_background_tasks(adapter: WebhookAdapter) -> None:
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ===================================================================
# Core behaviour: event fires the cron job, not a webhook session
# ===================================================================

class TestCronJobTrigger:
    @pytest.mark.parametrize("route_name", ["meeting-lifecycle", "comms-intake"])
    @pytest.mark.asyncio
    async def test_standalone_project_route_accepts_only_its_self_prefix(self, route_name):
        """A named standalone home treats /p/self/ as its exact Project route."""
        adapter = _make_adapter({
            route_name: {
                "profile": "project-fixture",
                "secret": _INSECURE_NO_AUTH,
                "cron_job": "project-meeting-lifecycle",
                "prompt": "Process {meeting_id}",
            }
        })
        app = _create_app(adapter)
        app.router.add_post("/p/{profile}/webhooks/{route_name}", adapter._handle_webhook)
        body = json.dumps({"event_type": "meeting.ended", "meeting_id": "one"}).encode()
        with (
            patch("hermes_cli.profiles.profile_matches_home", side_effect=lambda p: p == "project-fixture"),
            patch("tools.cronjob_tools.execute_job_for_event", return_value={
                "claimed": True, "success": True, "error": None
            }),
        ):
            async with TestClient(TestServer(app)) as cli:
                bare = await cli.post(f"/webhooks/{route_name}", data=body)
                assert bare.status == 404
                foreign = await cli.post(
                    f"/p/project-foreign/webhooks/{route_name}", data=body
                )
                assert foreign.status == 404
                own = await cli.post(
                    f"/p/project-fixture/webhooks/{route_name}", data=body
                )
                assert own.status == 202
                await _drain_background_tasks(adapter)

    @pytest.mark.asyncio
    async def test_meeting_burst_retries_same_delivery_after_busy_run(self):
        """A second meeting event must remain retryable while the cron job runs."""
        adapter = _make_adapter({
            "meeting-lifecycle": {
                "secret": _INSECURE_NO_AUTH,
                "cron_job": "project-meeting-lifecycle",
                "prompt": "Process {meeting_id}",
            }
        })
        started = threading.Event()
        release = threading.Event()
        fired = []

        def _fake_execute(job_ref, extra_prompt=None):
            fired.append((job_ref, extra_prompt))
            if len(fired) == 1:
                started.set()
                assert release.wait(5)
            return {"claimed": True, "success": True, "error": None}

        body_a = json.dumps({"event_type": "meeting.ended", "meeting_id": "a"}).encode()
        body_b = json.dumps({"event_type": "meeting.ended", "meeting_id": "b"}).encode()
        with patch("tools.cronjob_tools.execute_job_for_event", side_effect=_fake_execute):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                first = await cli.post(
                    "/webhooks/meeting-lifecycle",
                    data=body_a,
                    headers={"X-Request-ID": "meeting-a"},
                )
                assert first.status == 202
                assert await asyncio.to_thread(started.wait, 2)

                busy = await cli.post(
                    "/webhooks/meeting-lifecycle",
                    data=body_b,
                    headers={"X-Request-ID": "meeting-b"},
                )
                assert busy.status == 503
                assert busy.headers["Retry-After"] == "30"
                release.set()
                await _drain_background_tasks(adapter)

                retry = await cli.post(
                    "/webhooks/meeting-lifecycle",
                    data=body_b,
                    headers={"X-Request-ID": "meeting-b"},
                )
                assert retry.status == 202
                await _drain_background_tasks(adapter)
        assert len(fired) == 2
        assert "Process b" in fired[1][1]

    @pytest.mark.asyncio
    async def test_serialized_comms_route_retries_while_prior_run_is_busy(self):
        """Comms delivery can opt into the same retryable serialized cron admission."""
        adapter = _make_adapter({
            "comms-intake": {
                "secret": _INSECURE_NO_AUTH,
                "cron_job": "project-comms-intake",
                "serialize_cron_job": True,
                "prompt": "Process {source_ids}",
            }
        })
        started = threading.Event()
        release = threading.Event()

        def _fake_execute(_job_ref, _extra_prompt=None):
            started.set()
            assert release.wait(5)
            return {"claimed": True, "success": True, "error": None}

        with patch("tools.cronjob_tools.execute_job_for_event", side_effect=_fake_execute):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                first = await cli.post(
                    "/webhooks/comms-intake",
                    json={"event_type": "email.ingested", "source_ids": ["email:a"]},
                    headers={"X-Request-ID": "comms-a"},
                )
                assert first.status == 202
                assert await asyncio.to_thread(started.wait, 2)
                busy = await cli.post(
                    "/webhooks/comms-intake",
                    json={"event_type": "email.ingested", "source_ids": ["email:b"]},
                    headers={"X-Request-ID": "comms-b"},
                )
                assert busy.status == 503
                release.set()
                await _drain_background_tasks(adapter)

    @pytest.mark.asyncio
    async def test_post_fires_job_with_event_context(self):
        routes = {
            "pr-feedback": {
                "secret": _INSECURE_NO_AUTH,
                "cron_job": "review-sweeper",
                "prompt": "PR #{number} received feedback: {review.body}",
            }
        }
        adapter = _make_adapter(routes)

        handle_message_calls = []

        async def _capture(event):
            handle_message_calls.append(event)

        adapter.handle_message = _capture

        fired = []

        def _fake_execute(job_ref, extra_prompt=None):
            fired.append((job_ref, extra_prompt))
            return {"claimed": True, "success": True, "error": None}

        app = _create_app(adapter)
        body = json.dumps(
            {"number": 7, "review": {"body": "needs tests"}}
        ).encode()

        with patch(
            "tools.cronjob_tools.execute_job_for_event",
            side_effect=_fake_execute,
        ):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/pr-feedback",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Delivery": "delivery-cron-1",
                        "X-GitHub-Event": "pull_request_review",
                    },
                )
                assert resp.status == 202
                data = await resp.json()
                assert data["status"] == "accepted"
                assert data["cron_job"] == "review-sweeper"
                await _drain_background_tasks(adapter)

        # Job fired exactly once with the rendered prompt as run context
        assert len(fired) == 1
        job_ref, extra_prompt = fired[0]
        assert job_ref == "review-sweeper"
        assert "PR #7 received feedback: needs tests" in extra_prompt
        assert "pull_request_review" in extra_prompt  # event provenance

        # No fresh webhook agent session was started
        assert handle_message_calls == []

    @pytest.mark.asyncio
    async def test_job_failure_does_not_break_http_response(self):
        routes = {
            "flaky": {"secret": _INSECURE_NO_AUTH, "cron_job": "gone-job"}
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)

        def _fail(job_ref, extra_prompt=None):
            return {
                "claimed": False,
                "success": False,
                "error": "Cron job 'gone-job' not found.",
            }

        with patch(
            "tools.cronjob_tools.execute_job_for_event", side_effect=_fail
        ):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/webhooks/flaky",
                    data=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Delivery": "delivery-cron-2",
                    },
                )
                # Fire-and-forget: the POST is accepted even when the job
                # later fails; the failure is logged, not surfaced.
                assert resp.status == 202
                await _drain_background_tasks(adapter)


    @pytest.mark.asyncio
    async def test_routed_profile_scope_reaches_the_job_run(self, tmp_path, monkeypatch):
        """/p/<profile>/ routes must fire the job from THAT profile's cron store, not the gateway's
        default home (and route-level skills stay out of the per-run context — the job's own apply)."""
        home = tmp_path / ".hermes"
        (home / "profiles" / "sec").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: home)
        monkeypatch.setattr("hermes_cli.profiles._get_profiles_root", lambda: home / "profiles")
        adapter = _make_adapter({"ev": {"secret": _INSECURE_NO_AUTH, "cron_job": "sweeper", "profile": "sec",
                                        "skills": ["some-skill"], "prompt": "hello {n}"}})
        monkeypatch.setattr(adapter, "_resolve_request_profile", lambda request: "sec")
        monkeypatch.setattr(adapter, "_apply_skills", lambda prompt, skills: pytest.fail("skills applied"))
        seen = []

        def _fake_execute(job_ref, extra_prompt=None):
            from hermes_constants import get_hermes_home
            seen.append((get_hermes_home(), extra_prompt))
            return {"claimed": True, "success": True, "error": None}

        with patch("tools.cronjob_tools.execute_job_for_event", side_effect=_fake_execute):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post("/webhooks/ev", data=b'{"n": 1}',
                                      headers={"Content-Type": "application/json", "X-GitHub-Delivery": "d-3"})
                assert resp.status == 202
                await _drain_background_tasks(adapter)
        assert seen and seen[0][0] == (home / "profiles" / "sec").resolve()
        assert "hello 1" in seen[0][1]


# ===================================================================
# Startup validation
# ===================================================================

class TestCronJobRouteValidation:
    @pytest.mark.asyncio
    async def test_cron_job_plus_deliver_only_rejected_at_connect(self):
        routes = {
            "bad": {
                "secret": "s3cret",
                "cron_job": "some-job",
                "deliver_only": True,
                "deliver": "telegram",
            }
        }
        adapter = _make_adapter(routes)
        with pytest.raises(ValueError, match="mutually exclusive"):
            await adapter.connect()


# ===================================================================
# execute_job_for_event unit behaviour
# ===================================================================

class TestExecuteJobForEvent:
    def test_unknown_job_returns_error(self):
        from tools import cronjob_tools

        with patch.object(cronjob_tools, "resolve_job_ref", return_value=None):
            result = cronjob_tools.execute_job_for_event("nope")
        assert result["claimed"] is False
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_ambiguous_ref_returns_error(self):
        from cron.jobs import AmbiguousJobReference
        from tools import cronjob_tools

        with patch.object(
            cronjob_tools,
            "resolve_job_ref",
            side_effect=AmbiguousJobReference(
                "x", [{"id": "job-a"}, {"id": "job-b"}]
            ),
        ):
            result = cronjob_tools.execute_job_for_event("x")
        assert result["claimed"] is False
        assert result["success"] is False
        assert "ambiguous" in result["error"].lower()

    def test_resolved_job_fires_with_extra_prompt(self):
        from tools import cronjob_tools

        job = {"id": "job-123", "name": "sweeper"}
        with patch.object(
            cronjob_tools, "resolve_job_ref", return_value=job
        ), patch.object(
            cronjob_tools,
            "_execute_job_now",
            return_value={"claimed": True, "success": True, "error": None},
        ) as mock_exec:
            result = cronjob_tools.execute_job_for_event(
                "sweeper", extra_prompt="event context"
            )
        assert result["success"] is True
        mock_exec.assert_called_once_with(job, extra_prompt="event context")
