"""Deterministic routing for the Orchestra-owned personal specialist umbrella.

This module deliberately has no transport side effects. An authenticated ingress
adapter resolves a message, submits an invocation, and stores the returned session
only after the API accepts it. Legacy specialist bot accounts are never aliases.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import yaml


_SELECTOR = re.compile(r"^\s*@([a-z][a-z0-9_-]*)(?=\s|$)", re.IGNORECASE)
_PERSONAL = frozenset({
    "champ", "geo", "research", "markettools", "pursuit", "finance", "health", "infra",
})


@dataclass(frozen=True)
class Conversation:
    frontend: str
    account: str
    conversation: str
    thread: str
    sender: str

    def __post_init__(self) -> None:
        if self.thread is None:
            object.__setattr__(self, "thread", "")
        if not all(isinstance(v, str) and v for v in (
            self.frontend, self.account, self.conversation, self.sender,
        )) or not isinstance(self.thread, str):
            raise ValueError("frontend, account, conversation and sender are required")


@dataclass(frozen=True)
class Decision:
    kind: str  # invoke, orchestra, suggest, clarify, ignore
    profile: Optional[str] = None
    prompt: Optional[str] = None
    session_id: Optional[str] = None
    choices: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Rules:
    owner_accounts: Mapping[str, str]
    room_defaults: Mapping[tuple[str, str, str], str]
    keywords: Mapping[str, tuple[str, ...]]

    @classmethod
    def load(cls, path: Path) -> "Rules":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("personal routing rules require version 1")
        raw_accounts = data.get("owner_accounts", {})
        raw_defaults = data.get("room_defaults", [])
        raw_keywords = data.get("keywords", {})
        if not isinstance(raw_accounts, dict) or not isinstance(raw_defaults, list) or not isinstance(raw_keywords, dict):
            raise ValueError("invalid personal routing rules")
        accounts: dict[str, str] = {}
        for frontend, account in raw_accounts.items():
            if not isinstance(frontend, str) or not isinstance(account, str) or not frontend or not account:
                raise ValueError("owner account must be an exact non-empty frontend/account pair")
            accounts[frontend] = account
        defaults: dict[tuple[str, str, str], str] = {}
        for row in raw_defaults:
            if not isinstance(row, dict):
                raise ValueError("room default must be a mapping")
            frontend, account, conversation, profile = (row.get(k) for k in (
                "frontend", "account", "conversation", "profile"))
            if not all(isinstance(v, str) and v for v in (frontend, account, conversation, profile)):
                raise ValueError("room default requires exact frontend, account, conversation and profile")
            if accounts.get(frontend) != account or profile not in _PERSONAL:
                raise ValueError("room default must belong to the owner account and personal allowlist")
            key = (frontend, account, conversation)
            if key in defaults:
                raise ValueError("duplicate room default")
            defaults[key] = profile
        keywords: dict[str, tuple[str, ...]] = {}
        for profile, phrases in raw_keywords.items():
            if profile not in _PERSONAL or not isinstance(phrases, list):
                raise ValueError("keywords must map allowlisted personal profiles to phrase lists")
            if not all(isinstance(p, str) and p.strip() and len(p) < 100 for p in phrases):
                raise ValueError("keyword phrases must be non-empty strings under 100 characters")
            keywords[profile] = tuple(p.casefold().strip() for p in phrases)
        return cls(accounts, defaults, keywords)


class BindingStore:
    """The profile and actual API session are scoped to every conversation discriminator."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS personal_bindings (
                frontend TEXT NOT NULL, account TEXT NOT NULL, conversation TEXT NOT NULL,
                thread TEXT NOT NULL, sender TEXT NOT NULL, profile TEXT NOT NULL,
                session_id TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (frontend, account, conversation, thread, sender)
            )""")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, ctx: Conversation) -> Optional[tuple[str, str]]:
        with self._connect() as db:
            row = db.execute(
                """SELECT profile, session_id FROM personal_bindings WHERE
                   frontend=? AND account=? AND conversation=? AND thread=? AND sender=?""",
                (ctx.frontend, ctx.account, ctx.conversation, ctx.thread, ctx.sender),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def commit(self, ctx: Conversation, profile: str, session_id: str) -> None:
        if profile not in _PERSONAL or not session_id or not isinstance(session_id, str):
            raise ValueError("binding needs an allowlisted profile and actual session ID")
        with self._connect() as db:
            db.execute("""INSERT INTO personal_bindings
                (frontend, account, conversation, thread, sender, profile, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(frontend, account, conversation, thread, sender) DO UPDATE SET
                profile=excluded.profile, session_id=excluded.session_id,
                updated_at=CURRENT_TIMESTAMP""",
                (ctx.frontend, ctx.account, ctx.conversation, ctx.thread, ctx.sender, profile, session_id),
            )


class Router:
    def __init__(self, rules: Rules, bindings: BindingStore):
        self.rules = rules
        self.bindings = bindings

    def resolve(self, ctx: Conversation, text: str, *, action_profile: Optional[str] = None) -> Decision:
        # The receiving account is supplied by trusted adapter state, never message text.
        if self.rules.owner_accounts.get(ctx.frontend) != ctx.account:
            return Decision("ignore", reason="account is not Orchestra's configured ingress")
        match = _SELECTOR.match(text)
        if match:
            profile = match.group(1).lower()
            if profile not in _PERSONAL:
                return Decision("clarify", choices=tuple(sorted(_PERSONAL)), reason="unknown @profile selector")
            prompt = text[match.end():].strip()
            if not prompt:
                return Decision("clarify", choices=(profile,), reason="selector needs a request")
            bound = self.bindings.get(ctx)
            return Decision("invoke", profile, prompt,
                            bound[1] if bound and bound[0] == profile else None, reason="explicit selector")
        # Only the trusted Orchestra action/tool boundary may populate action_profile.
        if action_profile is not None:
            if action_profile not in _PERSONAL:
                return Decision("clarify", choices=tuple(sorted(_PERSONAL)), reason="unknown action profile")
            bound = self.bindings.get(ctx)
            return Decision("invoke", action_profile, text,
                            bound[1] if bound and bound[0] == action_profile else None,
                            reason="explicit Orchestra action")
        bound = self.bindings.get(ctx)
        if bound:
            profile, session_id = bound
            if profile in _PERSONAL:
                return Decision("invoke", profile, text, session_id, reason="saved conversation binding")
            return Decision("clarify", reason="saved profile is no longer allowlisted")
        room_profile = self.rules.room_defaults.get((ctx.frontend, ctx.account, ctx.conversation))
        if room_profile:
            return Decision("invoke", room_profile, text, reason="configured room default")
        candidates = tuple(sorted(profile for profile, phrases in self.rules.keywords.items()
                                  if any(_phrase_present(text, phrase) for phrase in phrases)))
        if len(candidates) == 1:
            return Decision("suggest", candidates[0], text, reason="keyword suggestion only")
        if len(candidates) > 1:
            return Decision("clarify", choices=candidates, reason="ambiguous keywords")
        return Decision("orchestra", prompt=text, reason="no specialist route")


def _phrase_present(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text.casefold()))
