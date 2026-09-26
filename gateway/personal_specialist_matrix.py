"""Opt-in Orchestra Matrix ingress for on-demand personal specialists.

The existing Matrix adapter keeps authorization, mention, duplicate-event and
delivery handling. This wrapper only replaces an accepted text turn when the
versioned owner rules explicitly name the receiving account.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

from gateway.personal_specialist_routing import BindingStore, Conversation, Router, Rules
from gateway.platforms.event import MessageType


class InvocationUnavailable(Exception):
    """A request could not be resolved to a successful durable invocation."""


# The API permits 1 MiB of raw plain text. JSON escaping can expand control
# characters sixfold before the record envelope is added.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class InvocationClient:
    """Small, fixed-loopback client; bearer and prompts never enter logs or URLs."""

    def __init__(self, token_reader: Callable[[], str], *, port: int = 8789):
        if not 1 <= port <= 65535:
            raise ValueError("invalid loopback invocation port")
        self._token_reader = token_reader
        self._base = f"http://127.0.0.1:{port}"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | None:
        token = self._token_reader()
        if not token:
            raise InvocationUnavailable("missing Orchestra invocation credential")
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Hermes-Invocation-Response": "extended",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if method == "GET" and exc.code == 404:
                return None
            raise InvocationUnavailable(f"invocation API returned HTTP {exc.code}") from None
        except (OSError, TimeoutError) as exc:
            raise InvocationUnavailable("invocation API is unavailable") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise InvocationUnavailable("invocation API response exceeded the limit")
        try:
            record = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InvocationUnavailable("invalid invocation API response") from exc
        if not isinstance(record, dict):
            raise InvocationUnavailable("invalid invocation API response")
        return record

    async def run(self, *, key: str, profile: str, prompt: str,
                  session_id: str | None, timeout_seconds: int = 900) -> dict:
        # Look up before submit: a gateway crash after binding a finished turn
        # must not re-POST that event with a changed session_id and get a 409.
        by_key = "/v1/invocations/by-key/" + urllib.parse.quote(key, safe="")
        record = await asyncio.to_thread(self._request, "GET", by_key)
        if record is None:
            body: dict[str, Any] = {
                "idempotency_key": key, "profile": profile, "prompt": prompt,
                "timeout_seconds": timeout_seconds,
            }
            if session_id:
                body["session_id"] = session_id
            record = await asyncio.to_thread(self._request, "POST", "/v1/invocations", body)
        if not isinstance(record, dict) or record.get("profile") != profile:
            raise InvocationUnavailable("event key belongs to another profile")
        invocation_id = record.get("id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise InvocationUnavailable("invocation API returned no invocation ID")
        deadline = time.monotonic() + timeout_seconds + 30
        while record.get("status") in {"pending", "queued", "running"}:
            if time.monotonic() >= deadline:
                raise InvocationUnavailable("invocation remains unresolved")
            await asyncio.sleep(1)
            record = await asyncio.to_thread(
                self._request, "GET", "/v1/invocations/" + urllib.parse.quote(invocation_id, safe=""))
            if not isinstance(record, dict) or record.get("id") != invocation_id:
                raise InvocationUnavailable("invocation record changed during polling")
        if record.get("status") != "succeeded":
            raise InvocationUnavailable(f"specialist invocation {record.get('status', 'failed')}")
        result = record.get("result")
        actual_session = record.get("session_id")
        if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                or not result["text"].strip() or not isinstance(actual_session, str)
                or not actual_session):
            raise InvocationUnavailable("specialist returned no reply or session")
        return record


def event_key(ctx: Conversation, event_id: str) -> str:
    if not event_id:
        raise ValueError("Matrix event ID is required")
    raw = json.dumps((ctx.frontend, ctx.account, ctx.conversation, ctx.thread,
                      ctx.sender, event_id), ensure_ascii=False, separators=(",", ":"))
    return "matrix-personal:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MatrixPersonalRouter:
    def __init__(self, router: Router, client: InvocationClient, account: str,
                 account_getter: Callable[[], str] | None = None):
        self.router = router
        self.client = client
        self.account = account
        self.account_getter = account_getter or (lambda: account)

    async def handle(self, event: Any, fallback: Callable[[Any], Awaitable[Any]]) -> Any:
        # Matrix whoami can replace the configured account during connect().
        # Route only while the authenticated adapter still owns the exact rule.
        if self.account_getter() != self.account:
            return await fallback(event)
        if (event.message_type != MessageType.TEXT or not event.source
                or not isinstance(event.text, str)):
            return await fallback(event)
        source = event.source
        if not source.chat_id or not source.user_id:
            return await fallback(event)
        ctx = Conversation("matrix", self.account, source.chat_id,
                           source.thread_id or "", source.user_id or "")
        decision = self.router.resolve(ctx, event.text)
        if decision.kind in {"ignore", "orchestra"}:
            return await fallback(event)
        if decision.kind == "suggest":
            return f"This may fit {decision.profile}. Reply with @{decision.profile} followed by your request."
        if decision.kind == "clarify":
            names = ", ".join("@" + name for name in decision.choices)
            return f"Choose a personal specialist with an exact selector: {names}." if names else (
                "The saved specialist is unavailable; choose an exact @profile selector.")
        if decision.kind != "invoke" or not decision.profile or not decision.prompt:
            raise InvocationUnavailable("invalid specialist route")
        # The Matrix adapter has authorized this sender for Orchestra, but a
        # specialist may have a narrower existing allowlist. An absent policy
        # fails closed for that specialist, including saved bindings/defaults.
        if source.user_id not in self.router.rules.matrix_allowed_senders.get(decision.profile, frozenset()):
            return "This account is not allowed to use the selected specialist."
        try:
            key = event_key(ctx, event.message_id or "")
            record = await self.client.run(
                key=key, profile=decision.profile, prompt=decision.prompt,
                session_id=decision.session_id)
        except (InvocationUnavailable, ValueError):
            return "The specialist request could not be completed. Please retry after checking the invocation service."
        self.router.bindings.commit(ctx, decision.profile, record["session_id"])
        return f"[{decision.profile}]\n{record['result']['text']}"


def build_opt_in_router(adapter: Any) -> MatrixPersonalRouter | None:
    """Fail closed until versioned Matrix YAML, exact owner and bearer are configured."""
    from gateway.platforms._shared import get_scoped_secret
    from hermes_constants import get_hermes_home

    extra = getattr(getattr(adapter, "config", None), "extra", {}) or {}
    settings = extra.get("personal_routing")
    if settings is None:
        return None
    if not isinstance(settings, dict) or type(settings.get("version")) is not int or settings["version"] != 1:
        raise ValueError("personal routing config requires version 1")
    enabled = settings.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("personal routing enabled must be a boolean")
    if not enabled:
        return None
    rules_raw = settings.get("rules_path")
    if not isinstance(rules_raw, str) or not rules_raw or not Path(rules_raw).is_absolute():
        raise ValueError("personal routing needs absolute rules path")
    rules = Rules.load(Path(rules_raw))
    account = rules.owner_accounts.get("matrix")
    configured_account = str(getattr(adapter, "_user_id", "") or "")
    if not account or (configured_account and configured_account != account):
        raise ValueError("personal routing Matrix owner does not match this adapter")
    if not get_scoped_secret("HERMES_PERSONAL_INVOCATION_TOKEN", ""):
        raise ValueError("personal routing needs Orchestra invocation credential")
    port = settings.get("invocation_port", 8789)
    if type(port) is not int:
        raise ValueError("personal routing needs a valid loopback invocation port")
    if not 1 <= port <= 65535:
        raise ValueError("personal routing needs a valid loopback invocation port")
    home = Path(get_hermes_home())
    bindings = BindingStore(home / "state" / "personal-specialist-bindings.sqlite3")
    client = InvocationClient(
        lambda: str(get_scoped_secret("HERMES_PERSONAL_INVOCATION_TOKEN", "")),
        port=port,
    )
    return MatrixPersonalRouter(
        Router(rules, bindings), client, account,
        account_getter=lambda: str(getattr(adapter, "_user_id", "")),
    )
