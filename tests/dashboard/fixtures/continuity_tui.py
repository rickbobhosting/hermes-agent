"""Deterministic PTY child for the browser continuity acceptance test.

This process deliberately has no provider or gateway imports. Its only durable
state is the selected profile's disposable SessionDB.
"""

import os
import signal
import sys
import time
from pathlib import Path

from hermes_state import SessionDB


def emit(value: str) -> None:
    sys.stdout.write(value)
    sys.stdout.flush()


def main() -> None:
    home = Path(os.environ["HERMES_HOME"])
    session_id = os.environ["HERMES_TUI_RESUME"]
    db = SessionDB(home / "state.db")
    try:
        if db.get_session(session_id) is None:
            db.create_session(session_id, "cli")
        previous = db.get_messages(session_id)
        for message in previous:
            emit(f"{message['content']}\r\n")
        emit("fixture ready> ")

        def cancel(_signum, _frame):
            emit("\r\nfixture cancelled\r\n")
            raise InterruptedError

        signal.signal(signal.SIGINT, cancel)
        while line := sys.stdin.readline():
            prompt = line.strip()
            if not prompt:
                emit("fixture ready> ")
                continue
            if prompt == "slow fixture turn":
                emit("fixture pending")
                try:
                    time.sleep(10)
                except InterruptedError:
                    pass
                emit("\r\nfixture ready> ")
                continue
            db.append_message(session_id, "user", prompt)
            reply = f"fixture reply {db.get_session(session_id)['message_count'] // 2 + 1}: {prompt}"
            db.append_message(session_id, "assistant", reply)
            for part in ("fixture ", reply.removeprefix("fixture ")):
                emit(part)
                # Keep the candidate's first chunk distinct on the browser
                # WebSocket so the handoff fixture can detect buffering.
                time.sleep(0.35 if prompt == "candidate turn one" else 0.08)
            emit("\r\nfixture ready> ")
    finally:
        db.close()


if __name__ == "__main__":
    main()
