# Hermes runtime worktree union inventory — 2026-09-26

This compares the retained worktrees with the live integration checkout and the isolated runtime candidate. It does not authorize removing a worktree or changing a running service.

| Tree | HEAD | Common ancestor with live `d177b119e9` | Commits unmatched by live | Working state |
|---|---|---|---:|---|
| `dashboard-fleet` | `edf30ca29f` | `18e840469f` | 2 | 13 tracked edits, 80 untracked files |
| `pty-hardening` | `826dda94ed` | `18e840469f` | 4 | Clean |
| Runtime candidate | `9c0f45705c` | Live `d177b119e9` | 7 candidate commits | Clean |

The two worktree branches share `9f444b719e` (hide external Claude Code credentials) and `edf30ca29f` (render the Basic password login form instead of entering the OAuth start path). The candidate already implements the password behavior in `dashboard_auth/middleware.py`. Candidate commit `9c0f45705c` carries the credential display behavior into the refactored OAuth modules and desktop provider list; 37 focused OAuth/auth tests pass.

`dashboard-fleet` has additional uncommitted behavior:

- `dashboard.fleet_targets` names explicit, absolute profile homes; selectors are `fleet:<id>`, with `inspect`, `manage`, and `execute` levels. Unregistered selectors fail closed.
- Dashboard profile discovery, session, cron, memory, and other APIs expose or scope those homes. Middleware restricts unsafe HTTP requests according to target access; frontend pages hide management controls according to capability.
- An early server-owned PTY registry lets a browser reconnect to a bounded output buffer after a WebSocket disconnect. Its registry uses detached-session expiry and closes all PTYs at server shutdown.
- `research/fox-island/` contains 71 untracked files (source documents, text, images, and HTML). The other nine untracked files are code/tests for fleet access and PTY attachment. Preserve all 80 files.

`pty-hardening` adds two more commits beyond the shared pair:

- `e8653f4290` keeps dashboard chat PTYs alive after browser disconnect.
- `826dda94ed` expands that mechanism across 47 files: bounded browser output and input queues, explicit retained PTY list/stop controls, frontend reattachment and event handling, process/session lease ownership across dashboard, CLI, and TUI, and bridge cancellation behavior. It changes 6,769 lines added and 567 removed relative to its parent.

The candidate currently has the earlier PTY registry, but not the later retained-task controls or the cross-surface ownership hardening. Its `web_server.py` is split into separate router modules and its chat frontend has diverged substantially, so a broad cherry-pick of either dirty dashboard code or `826dda94ed` would not constitute a reviewed union. In particular, neither registry can transfer a PTY between dashboard processes at service handoff; the old process must retain its existing connections until they drain.

The candidate now parses explicit `dashboard.fleet_targets`, lists registered homes as inspect-only cards on Profiles, and exposes an authenticated read-only session list at `/api/profiles/fleet/{id}/sessions`. The global profile selector excludes these targets and the generic profile resolver still rejects `fleet:` names, so the candidate cannot change or execute in those homes. The source worktree's scoped config, memory, cron, skill, and management surfaces remain unported. The new session list is an API inspection surface; browser history/detail and chat continuation for fleet homes remain a frontend integration gap. Configured `manage` or `execute` access is deliberately not granted by this candidate.

Before either source worktree qualifies for removal, integrate and test the missing behavior against the candidate, preserve the dashboard dirty patch and every untracked file in a verified archive, reconcile any product differences, prove both authenticated chat frontends and connection drain, then verify no live process or unit references the source checkout. `pty-hardening` is clean but has unmatched commits; `dashboard-fleet` is neither clean nor semantically merged. Both stay retained.
