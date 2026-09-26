# Candidate Orchestra Matrix routing

The primary Matrix adapter can route an accepted text event to one of eight
personal specialists through the loopback Invocation API. It remains disabled
unless Orchestra's `config.yaml` has this versioned Matrix block:

```yaml
matrix:
  personal_routing:
    version: 1
    enabled: true
    rules_path: /absolute/path/to/personal-specialist-routing.v1.yaml
    invocation_port: 8789
```

`invocation_port` is optional and defaults to `8789`. It may select a
separately staged loopback Invocation API during the phased handoff; it must
be an integer from 1 to 65535. `enabled` must be a YAML boolean. Without the
block, or when `enabled: false`, the existing Matrix handler remains in use.
The gateway's scoped `HERMES_PERSONAL_INVOCATION_TOKEN` environment variable
provides the bearer. The versioned rules file must name the exact
receiving Orchestra Matrix account. Missing or mismatched configuration fails
gateway startup rather than silently dispatching to another bot. The bearer is
read from the gateway's profile-scoped environment at request time and is
never included in a URL or log line.
The client bypasses process proxy settings and HTTP redirects so its bearer
and prompt stay on `127.0.0.1`.

The adapter still owns Matrix authorization, mention/room policy, event dedup,
threading, and reply delivery. Routing is installed only on a standalone
primary Matrix adapter, leaving secondary, work, and Qwen handlers unchanged.
When enabled, Matrix text batching is disabled on that adapter so each event
has its own idempotency key. The key binds account, room, thread, sender, and
Matrix event ID. A repeated event first retrieves its durable invocation by
key. On success the binding database at
`$HERMES_HOME/state/personal-specialist-bindings.sqlite3` stores the actual
API session ID for that conversation. A different room, thread, or sender
cannot resume it. Specialist replies have a `[profile]` label and are sent by
the Orchestra account.

An exact leading `@profile` selector takes precedence over the saved binding
and room default. Keywords only suggest a selector; overlapping suggestions
ask the user to choose. Unmatched messages continue through Orchestra's
existing handler. Gateway slash commands, media, and other Matrix event types
also retain their existing handler. `Router.resolve(action_profile=...)` supports
a future trusted Orchestra tool boundary; inbound Matrix text never supplies
that privileged parameter.

This code is a candidate, not an activation instruction. Before enabling the
block, migrate/allowlist the eight targets in the candidate Invocation API,
install its compatible personal prose result contract, verify Orchestra room
membership and sole reply ownership, establish authenticated chat canaries for
both frontends, and prove an uninterrupted queue and frontend handoff. The
rules file deliberately ships with no owner account. Failed or uncertain
invocations return an error and do not switch to another model or profile.
The existing gateway delivery ledger provides its documented at-least-once
recovery semantics; a crash during Matrix send can still produce a visibly
marked recovered reply, so strict exactly-once delivery is not claimed.
