"""Tests for the skip_background_review constructor flag.

Verifies that AIAgent can skip the end-of-turn review, while named Project
closeout cron sessions opt in to durable learning.
"""
from __future__ import annotations

from unittest.mock import MagicMock
from pathlib import Path
from threading import Event, Thread

from run_agent import AIAgent
from agent.turn_finalizer import finalize_turn


def _make_agent(skip_background_review: bool = False) -> AIAgent:
    """Construct a minimally-configured AIAgent for unit testing."""
    return AIAgent(
        model="openai/gpt-4o-mini",
        provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=skip_background_review,
        platform="cli",
    )


def _stub_agent_for_finalize(agent: AIAgent) -> None:
    """Stub the heavy finalizer dependencies to isolate the review gate."""
    agent._spawn_background_review = MagicMock()
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    agent._persist_session = MagicMock()
    agent._session_messages = []
    agent._file_mutation_verifier_enabled = lambda: False
    agent.clear_interrupt = MagicMock()
    agent._stream_callback = None
    agent._sync_external_memory_for_turn = MagicMock()
    agent._skill_nudge_interval = 10
    agent._iters_since_skill = 20  # exceeds nudge interval → _should_review_skills = True
    agent.valid_tool_names = {"skill_manage"}
    agent.iteration_budget = MagicMock()
    agent.iteration_budget.remaining = 100
    agent.iteration_budget.used = 5
    agent.iteration_budget.max_total = 100
    agent.max_iterations = 50
    agent._emit_status = MagicMock()
    agent._safe_print = MagicMock()
    agent._apply_persist_user_message_override = MagicMock()
    agent.context_compressor = None
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    agent.model = "test-model"
    agent.session_id = "test-session"
    agent.quiet_mode = True
    agent._turn_failed_file_mutations = {}
    agent._db_flush_scan_prefix = None


def _run_finalize(agent: AIAgent) -> None:
    """Call finalize_turn with conditions that would trigger background review."""
    finalize_turn(
        agent,
        final_response="ok",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "assistant", "content": "ok"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="test-turn",
        user_message="test",
        original_user_message="test",
        _should_review_memory=True,
        _turn_exit_reason="text_response(1)",
    )


def test_default_skip_background_review_is_false() -> None:
    """Without an explicit override, AIAgent does NOT skip background review."""
    agent = _make_agent()
    assert agent.skip_background_review is False


def test_skip_background_review_flag_persists() -> None:
    """Passing skip_background_review=True records the flag on the instance."""
    agent = _make_agent(skip_background_review=True)
    assert agent.skip_background_review is True


def test_finalize_turn_skips_review_when_flag_set() -> None:
    """finalize_turn must NOT call _spawn_background_review when skip_background_review=True.

    Exercises the actual finalizer call path (not a duplicated guard expression)
    so it catches divergence between the production guard and the test.
    """
    agent = _make_agent(skip_background_review=True)
    _stub_agent_for_finalize(agent)
    _run_finalize(agent)
    agent._spawn_background_review.assert_not_called()


def test_finalize_turn_fires_review_when_flag_unset() -> None:
    """Counterpart: with the flag off, finalize_turn DOES call _spawn_background_review."""
    agent = _make_agent(skip_background_review=False)
    _stub_agent_for_finalize(agent)
    _run_finalize(agent)
    agent._spawn_background_review.assert_called_once()


def test_project_short_chat_reviews_both_stores_without_nudges() -> None:
    agent = _make_agent(skip_background_review=False)
    _stub_agent_for_finalize(agent)
    agent._review_on_completed_turn = True
    agent._iters_since_skill = 0
    finalize_turn(
        agent,
        final_response="ok",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[{"role": "assistant", "content": "ok"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="short-chat",
        user_message="correction",
        original_user_message="correction",
        _should_review_memory=False,
        _turn_exit_reason="text_response(1)",
    )
    agent._spawn_background_review.assert_called_once()
    assert agent._spawn_background_review.call_args.kwargs["review_memory"] is True
    assert agent._spawn_background_review.call_args.kwargs["review_skills"] is True


def test_failed_short_chat_does_not_review() -> None:
    agent = _make_agent(skip_background_review=False)
    _stub_agent_for_finalize(agent)
    agent._review_on_completed_turn = True
    agent._iters_since_skill = 0
    finalize_turn(
        agent,
        final_response="",
        api_call_count=1,
        interrupted=False,
        failed=True,
        messages=[{"role": "user", "content": "correction"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="failed-chat",
        user_message="correction",
        original_user_message="correction",
        _should_review_memory=False,
        _turn_exit_reason="session_persistence_failed",
    )
    agent._spawn_background_review.assert_not_called()


def test_cron_review_is_limited_to_project_closeout_jobs(monkeypatch) -> None:
    """Only the two Project closeouts get a durable session review."""
    from cron.scheduler import _CronAgentSetup, _construct_cron_agent

    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: Path("/tmp/project-fwmb"))

    setup = _CronAgentSetup(model="test", runtime={"provider": "test"})
    for name, expected in (
        ("project-end-of-day", True),
        ("project-friday-weekly", True),
        ("project-morning-checkin", False),
        ("other-friday-weekly", False),
    ):
        factory = MagicMock()
        agent = factory.return_value
        _construct_cron_agent(
            factory, {"name": name}, {}, setup,
            workdir=None, session_id="cron-test", session_db=None,
        )
        assert factory.call_args.kwargs["skip_background_review"] is not expected
        if expected:
            assert agent._review_on_completed_turn is True
            assert agent._background_review_no_defer is True

    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: Path("/tmp/personal"))
    factory = MagicMock()
    _construct_cron_agent(
        factory, {"name": "project-end-of-day"}, {}, setup,
        workdir=None, session_id="cron-personal", session_db=None,
    )
    assert factory.call_args.kwargs["skip_background_review"] is True


def test_ephemeral_teardown_waits_for_review_completion() -> None:
    from agent.background_review import wait_for_background_review

    finished = Event()
    agent = MagicMock()
    agent._background_review_run.request_done = finished
    agent._background_review_lock = None
    agent.session_id = "project-fwmb-closeout"
    worker = Thread(target=lambda: finished.set())
    worker.start()
    assert wait_for_background_review(agent, timeout_seconds=1) is True
    worker.join()


def test_persistence_failure_error_fallback_is_pinned_and_leaves_final_response_empty(monkeypatch, tmp_path) -> None:
    """With no model text, result["error"] carries a profile-pinned `hermes doctor`, while the
    memory sync and the background-review gate still see the turn as having produced nothing."""
    from hermes_constants import profile_cli_selector

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "research"))
    selector = profile_cli_selector()
    assert selector.strip()
    agent = _make_agent()
    _stub_agent_for_finalize(agent)
    # Force the fallback: the explainer normally supplies the text, so an empty explainer is
    # the only way the hardcoded copy reaches the user.
    monkeypatch.setattr(AIAgent, "_format_turn_completion_explanation", staticmethod(lambda *a, **k: ""))
    result = finalize_turn(
        agent,
        final_response="",
        api_call_count=1,
        interrupted=False,
        failed=True,
        messages=[{"role": "user", "content": "hi"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="test-turn",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=True,
        _turn_exit_reason="session_persistence_failed",
    )
    assert f"`hermes {selector}doctor`" in result["error"]
    assert agent._sync_external_memory_for_turn.call_args.kwargs["final_response"] == ""
    agent._spawn_background_review.assert_not_called()
