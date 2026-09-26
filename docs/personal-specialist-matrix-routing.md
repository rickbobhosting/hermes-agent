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
    activation_path: /absolute/path/to/personal-specialist-active.v1.json
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
When Matrix starts with only an access token, `whoami` fills its account after
the handlers are wired. The rules provide the expected account at startup;
events use the existing Matrix handler until the authenticated account matches
it. A configured account that contradicts the rules fails startup.
The client bypasses process proxy settings and HTTP redirects so its bearer
and prompt stay on `127.0.0.1`.

The required owner-only activation file is reread for each selected
specialist turn. Start with `{"version":1,"enabled_profiles":[]}`. Add one
allowlisted profile only after its old gateway has stopped and Orchestra
exclusively owns the reply room; removing it blocks new invocations without a
gateway restart. A missing, malformed, or permission-weakened file fails
closed.

The adapter still owns Matrix authorization, mention/room policy, event dedup,
threading, and reply delivery. Routing is installed only on a standalone
primary Matrix adapter, leaving secondary, work, and Qwen handlers unchanged.
Each selected specialist also needs an explicit sender list in the versioned
rules file, for example:

```yaml
matrix_allowed_senders:
  geo: ['@approved-user:example.org']
```

This check applies after Orchestra's ingress authorization to exact selectors,
room defaults, and saved bindings. A missing specialist entry denies its
invocation. The candidate does not import a different bot's allowlist at
runtime; review the selected specialist profile `.env` and approved access
policy before populating each entry. The September 26 read-only audit found
Orchestra's selected allowlist has ten senders, while each specialist has one
or two. Several specialist senders are outside Orchestra's current allowlist,
so both policies must be reconciled before those users can reach the umbrella.
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
