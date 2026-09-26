# Dashboard chat continuity acceptance fixture

Run from the candidate checkout with Python 3.12+, Node dependencies, and a
Chromium browser available (system `google-chrome`/`chromium`, or Playwright's
installed Chromium):

```bash
npm run build -w web
python3.12 -m pytest -q tests/dashboard/test_chat_continuity_browser.py
```

The test binds a temporary FastAPI listener to `127.0.0.1`, boots the real
dashboard bundle, and opens it in Playwright. It verifies that the uncredentialed
session API returns 401 under the dashboard's loopback session-token gate,
uses the rendered profile and chat session selectors, then drives the PTY
WebSocket through the terminal. A small PTY
child streams deterministic text and writes only to a temporary named profile's
`state.db`. The browser verifies two turns, Ctrl-C cancellation, a subsequent
turn in the same session, session history through the authenticated API, and
refresh replay. The sidebar's separate `/api/ws` status connection is closed
inside Playwright, so it cannot construct an agent or call a model.

Pytest's temporary directory owns `HOME`, `HERMES_HOME`, XDG paths, the named
profile, PTY child, and session store. The fixture clears inherited Hermes
launch flags before importing the server and gives the PTY child and browser
minimal environments. The listener and browser close at teardown. The test
uses the candidate's ignored `hermes_cli/web_dist` build output.

This proves the candidate loopback UI/PTY path with a fixed fake child. It
does not exercise the public Basic-auth/Cloudflare path, a model provider, the
real Hermes TUI, a mid-socket reconnect, or cross-session cancellation. Public
authenticated canaries and live connection-affinity tests remain mandatory.
