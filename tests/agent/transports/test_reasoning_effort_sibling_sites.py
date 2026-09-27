"""Sibling-site coverage for the reasoning-effort wire-vocabulary class (#89503).

The chat-completions chokepoint fix (ultra → max for every model) is covered
by tests/agent/test_reasoning_effort_wire_translation.py. These tests pin the
sibling sites fixed in the same sweep:

- Kimi/Moonshot top-level ``reasoning_effort``: K3 accepts low/high/max only
  (docs: default high); K2-era models accept low/medium/high. Previously the
  transport forwarded only {low,medium,high} and silently dropped everything
  else to "medium", so K3 400'd on "medium" requests and ultra resolved
  WEAKER than an explicit high (ladder inversion).
- Tencent TokenHub: accepts low/medium/high; upper-ladder levels previously
  dropped to the "high" default (accidentally right) but "minimal" also
  dropped to high — asked for the least, got the most.
- Codex/Responses transport: ultra → max for EVERY model, not just gpt-5.6.
"""

import pytest

from agent.transports import get_transport
import agent.transports.chat_completions  # noqa: F401
import agent.transports.codex  # noqa: F401


def _cc():
    return get_transport("chat_completions")


def _kimi_kwargs(model, reasoning_config):
    return _cc().build_kwargs(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        is_kimi=True,
        reasoning_config=reasoning_config,
    )


class TestKimiEffortVocabulary:
    def test_k3_maps_full_hermes_ladder(self):
        expected = {
            "minimal": "low",
            "low": "low",
            "medium": "high",
            "high": "high",
            "xhigh": "max",
            "max": "max",
            "ultra": "max",
        }
        for hermes_level, wire_level in expected.items():
            kw = _kimi_kwargs(
                "kimi-k3", {"enabled": True, "effort": hermes_level}
            )
            assert kw["reasoning_effort"] == wire_level, hermes_level

    def test_k3_default_is_high(self):
        kw = _kimi_kwargs("kimi-k3", None)
        assert kw["reasoning_effort"] == "high"

    def test_k2_upper_ladder_caps_at_high_not_medium(self):
        """Pre-fix, ultra/max/xhigh on K2-era models silently dropped to the
        'medium' default — the strongest ask resolved weaker than an explicit
        high (ladder inversion, same class as #74295)."""
        for level in ("xhigh", "max", "ultra"):
            kw = _kimi_kwargs(
                "moonshotai/kimi-k2.6", {"enabled": True, "effort": level}
            )
            assert kw["reasoning_effort"] == "high", level

    def test_k2_native_levels_pass_through(self):
        for level in ("low", "medium", "high"):
            kw = _kimi_kwargs(
                "moonshotai/kimi-k2.6", {"enabled": True, "effort": level}
            )
            assert kw["reasoning_effort"] == level

    def test_k2_minimal_maps_to_low(self):
        kw = _kimi_kwargs(
            "moonshotai/kimi-k2.6", {"enabled": True, "effort": "minimal"}
        )
        assert kw["reasoning_effort"] == "low"

    def test_disabled_omits_effort(self):
        kw = _kimi_kwargs("kimi-k3", {"enabled": False})
        assert "reasoning_effort" not in kw


class TestTokenHubEffortVocabulary:
    def _kwargs(self, reasoning_config):
        return _cc().build_kwargs(
            model="hunyuan-t2",
            messages=[{"role": "user", "content": "hi"}],
            is_tokenhub=True,
            reasoning_config=reasoning_config,
        )

    def test_upper_ladder_caps_at_high(self):
        for level in ("xhigh", "max", "ultra"):
            kw = self._kwargs({"enabled": True, "effort": level})
            assert kw["reasoning_effort"] == "high", level

    def test_minimal_maps_to_low_not_high(self):
        """Pre-fix, 'minimal' fell through to the 'high' default — asked for
        the least reasoning, got the most."""
        kw = self._kwargs({"enabled": True, "effort": "minimal"})
        assert kw["reasoning_effort"] == "low"

    def test_native_levels_pass_through(self):
        for level in ("low", "medium", "high"):
            kw = self._kwargs({"enabled": True, "effort": level})
            assert kw["reasoning_effort"] == level


class TestCodexUltraForEveryModel:
    def test_ultra_never_leaks_and_respects_per_model_ceiling(self):
        """'ultra' must never reach the Codex wire. Live-verified vocabulary
        (#68365): gpt-5.6 accepts max (its ceiling), gpt-5.5/o5 do not —
        their ceiling is xhigh."""
        transport = get_transport("codex_responses")
        expected = {
            "gpt-5.6-codex": "max",
            "o5-pro": "xhigh",
            "gpt-5.5": "xhigh",
            "some-responses-model": "xhigh",
        }
        for model, wire in expected.items():
            kw = transport.build_kwargs(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                reasoning_config={"enabled": True, "effort": "ultra"},
            )
            assert kw["reasoning"]["effort"] == wire, model

    @pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6-sol"])
    def test_gpt6_max_only_on_verified_routes(self, model):
        transport = get_transport("codex_responses")
        common = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "reasoning_config": {"enabled": True, "effort": "max"},
        }
        official = transport.build_kwargs(**common, provider="openai", base_url="https://api.openai.com/v1")
        assert official["model"] == model
        assert official["reasoning"]["effort"] == "max"
        verified = transport.build_kwargs(
            **common, provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex", is_codex_backend=True,
        )
        assert verified["reasoning"]["effort"] == ("max" if model == "gpt-6-luna" else "xhigh")
        for route in (
            {"provider": "openai-codex", "base_url": "https://api.openai.com/v1", "is_codex_backend": True},
            {"provider": "openai-codex", "base_url": "https://chatgpt.com/backend-api/codex/v2", "is_codex_backend": True},
            {"base_url": "https://api.openai.com.example/v1"},
        ):
            other = transport.build_kwargs(**common, **route)
            assert other["reasoning"]["effort"] == "xhigh"
            overridden_reasoning = transport.build_kwargs(
                **common,
                **route,
                request_overrides={"reasoning": {"effort": "max", "summary": "auto"}},
            )
            assert overridden_reasoning["reasoning"]["effort"] == "xhigh"

        verified_override = transport.build_kwargs(
            **common, provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex", is_codex_backend=True,
            request_overrides={"reasoning": {"effort": "max", "summary": "auto"}},
        )
        assert verified_override["reasoning"]["effort"] == ("max" if model == "gpt-6-luna" else "xhigh")

        overridden = transport.build_kwargs(
            **common,
            provider="openai",
            base_url="https://api.openai.com/v1",
            request_overrides={"model": "gpt-5.5"},
        )
        assert overridden["model"] == "gpt-5.5"
        assert overridden["reasoning"]["effort"] == "xhigh"

    def test_unverified_gpt6_route_caps_declared_profile_max(self, monkeypatch):
        monkeypatch.setattr(
            "agent.transports.codex._profile_declared_efforts",
            lambda *_args: ("none", "low", "medium", "high", "xhigh", "max"),
        )
        kwargs = get_transport("codex_responses").build_kwargs(
            model="gpt-6-luna",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex/v2",
            is_codex_backend=True,
            reasoning_config={"enabled": True, "effort": "max"},
        )
        assert kwargs["reasoning"]["effort"] == "xhigh"
