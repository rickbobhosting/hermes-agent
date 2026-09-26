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
session API returns 401, uses the rendered profile and chat session selectors,
then drives the authenticated PTY WebSocket through the terminal. A small PTY
child streams deterministic text and writes only to a temporary named profile's
`state.db`. The browser verifies two turns, Ctrl-C cancellation, a subsequent
turn in the same session, session history through the authenticated API, and
refresh replay. The sidebar's separate `/api/ws` status connection is closed
inside Playwright, so it cannot construct an agent or call a model.

Pytest's temporary directory owns `HOME`, `HERMES_HOME`, the named profile,
PTY child, and session store. The listener and browser close at teardown. The
test uses the candidate's ignored `hermes_cli/web_dist` build output. If you
need to undo only this fixture, revert its commit; the temporary directory is
removed by pytest and no service configuration is changed.
