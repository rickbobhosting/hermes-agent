from dataclasses import replace
from pathlib import Path

import pytest

from gateway.personal_specialist_routing import BindingStore, Conversation, Router, Rules


def _router(tmp_path: Path, *, defaults=None, keywords=None):
    rules = Rules(
        {"matrix": "@orchestra:example.org", "dashboard": "dashboard-account"},
        defaults or {},
        keywords or {"pursuit": ("pursuit",), "research": ("research",), "infra": ("infra",)},
    )
    return Router(rules, BindingStore(tmp_path / "orchestra" / "personal-routes.sqlite3"))


def _ctx(**kwargs):
    return replace(Conversation("matrix", "@orchestra:example.org", "!room:example.org", "thread-a", "@ricky:example.org"), **kwargs)


def test_exact_selector_precedes_action_binding_and_keywords(tmp_path):
    router = _router(tmp_path)
    ctx = _ctx()
    router.bindings.commit(ctx, "research", "research-session")
    decision = router.resolve(ctx, "@pursuit research this bid", action_profile="infra")
    assert (decision.kind, decision.profile, decision.prompt, decision.session_id) == (
        "invoke", "pursuit", "research this bid", None)
    assert router.resolve(ctx, "@project-bd-pursuits review this").kind == "clarify"
    assert router.resolve(ctx, "@pursuit").kind == "clarify"
    assert router.bindings.get(ctx) == ("research", "research-session")


def test_action_then_saved_binding_then_room_default(tmp_path):
    ctx = _ctx()
    router = _router(tmp_path, defaults={(ctx.frontend, ctx.account, ctx.conversation): "finance"})
    assert router.resolve(ctx, "balance").profile == "finance"
    router.bindings.commit(ctx, "health", "health-session")
    assert router.resolve(ctx, "pursuit research").profile == "health"
    action = router.resolve(ctx, "check", action_profile="infra")
    assert (action.profile, action.session_id) == ("infra", None)
    assert router.resolve(ctx, "follow up", action_profile="health").session_id == "health-session"


def test_bindings_are_scoped_by_every_conversation_discriminator(tmp_path):
    router = _router(tmp_path)
    ctx = _ctx()
    router.bindings.commit(ctx, "geo", "session-a")
    for other in (
        replace(ctx, frontend="dashboard", account="dashboard-account"),
        replace(ctx, account="@legacy-geo:example.org"),
        replace(ctx, conversation="!another:example.org"),
        replace(ctx, thread="thread-b"),
        replace(ctx, sender="@other:example.org"),
    ):
        assert router.bindings.get(other) is None
    assert router.bindings.get(ctx) == ("geo", "session-a")
    assert router.resolve(replace(ctx, account="@legacy-geo:example.org"), "@geo hi").kind == "ignore"
    assert router.resolve(ctx, "continue").session_id == "session-a"
    assert replace(ctx, thread=None).thread == ""


def test_keywords_only_suggest_and_collisions_clarify(tmp_path):
    router = _router(tmp_path)
    ctx = _ctx()
    assert router.resolve(ctx, "pursuit update").kind == "suggest"
    assert router.resolve(ctx, "research infra dependencies").choices == ("infra", "research")
    assert router.resolve(ctx, "I need inspiration").kind == "orchestra"  # no substring match
    assert router.resolve(ctx, "@project-bd-pursuits update pursuit").kind == "clarify"
    assert router.bindings.get(ctx) is None


def test_rules_loader_rejects_unknown_defaults_and_fails_closed_without_owner(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("""version: 1
owner_accounts: {matrix: '@orchestra:example.org'}
room_defaults:
  - {frontend: matrix, account: '@orchestra:example.org', conversation: '!room:example.org', profile: project-bd-pursuits}
""")
    with pytest.raises(ValueError, match="personal allowlist"):
        Rules.load(path)
    candidate_rules = Rules.load(Path(__file__).parents[2] / "config/personal-specialist-routing.v1.yaml")
    assert Router(candidate_rules, BindingStore(tmp_path / "state.sqlite3")).resolve(_ctx(), "@geo hi").kind == "ignore"


def test_matrix_sender_rules_require_exact_unique_personal_users(tmp_path):
    path = tmp_path / "rules.yaml"
    prefix = "version: 1\nowner_accounts: {matrix: '@orchestra:example.org'}\n"
    for raw in (
        "{geo: []}", "{geo: ['ricky:example.org']}",
        "{geo: ['@ricky:example.org', '@ricky:example.org']}",
        "{project-bd-pursuits: ['@ricky:example.org']}",
    ):
        path.write_text(prefix + f"matrix_allowed_senders: {raw}\n")
        with pytest.raises(ValueError, match="matrix allowed senders"):
            Rules.load(path)
    path.write_text(prefix + "matrix_allowed_senders: {geo: ['@ricky:example.org']}\n")
    assert Rules.load(path).matrix_allowed_senders["geo"] == frozenset({"@ricky:example.org"})


def test_reopen_store_preserves_exact_session_after_acceptance(tmp_path):
    path = tmp_path / "a" / "bindings.db"
    other_path = tmp_path / "b" / "bindings.db"
    ctx = _ctx()
    first = BindingStore(path)
    assert first.get(ctx) is None
    first.commit(ctx, "champ", "actual-api-session")
    assert BindingStore(other_path).get(ctx) is None
    assert BindingStore(path).get(ctx) == ("champ", "actual-api-session")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        first.commit(ctx, "project-bd-pursuits", "bad")
    assert BindingStore(path).get(ctx) == ("champ", "actual-api-session")
