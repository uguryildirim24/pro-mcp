"""Local command sessions for the user-authorized Mac connector.

Commands run as the server's macOS user, with that user's filesystem and network
access. This is deliberately not the read tools' project-path sandbox.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

MAX_OUTPUT = 1_000_000
MAX_SESSIONS = 32


@dataclass
class Session:
    process: subprocess.Popen
    output: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: threading.Event = field(default_factory=threading.Event)


sessions: dict[str, Session] = {}
registry_lock = threading.Lock()


def _collect(session: Session) -> None:
    try:
        while chunk := os.read(session.process.stdout.fileno(), 8192):
            with session.lock:
                session.output.extend(chunk)
                excess = len(session.output) - MAX_OUTPUT
                if excess > 0:
                    del session.output[:excess]
                    session.dropped += excess
    finally:
        session.process.wait()
        session.done.set()


def _result(session_id: str, session: Session, wait_ms: int) -> dict:
    session.done.wait(max(0, min(wait_ms, 10_000)) / 1000)
    with session.lock:
        output = bytes(session.output).decode('utf-8', errors='replace')
        session.output.clear()
        dropped, session.dropped = session.dropped, 0
    result = {'session_id': session_id, 'output': output,
              'exit_code': session.process.poll(), 'finished': session.done.is_set()}
    if dropped:
        result['truncated_bytes'] = dropped
    if result['finished']:
        with registry_lock:
            sessions.pop(session_id, None)
    return result


def execute(cmd: str, workdir: str | None = None, yield_time_ms: int = 1000) -> dict:
    cwd = Path(workdir).expanduser().resolve() if workdir else Path.home() / 'projects'
    if not cwd.is_dir():
        raise ValueError(f'Working directory does not exist: {cwd}')
    if not cmd.strip():
        raise ValueError('cmd must not be empty')
    with registry_lock:
        if len(sessions) >= MAX_SESSIONS:
            raise ValueError('32 command sessions are open; poll or stop existing sessions first')
        process = subprocess.Popen(
            ['/bin/zsh', '-c', cmd], cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        session = Session(process)
        session_id = uuid.uuid4().hex
        sessions[session_id] = session
    threading.Thread(target=_collect, args=(session,), daemon=True).start()
    return _result(session_id, session, yield_time_ms)


def interact(session_id: str, chars: str = '', yield_time_ms: int = 1000,
             terminate: bool = False, close_stdin: bool = False) -> dict:
    with registry_lock:
        session = sessions.get(session_id)
    if session is None:
        raise ValueError('Unknown or already completed command session')
    if terminate and session.process.poll() is None:
        os.killpg(session.process.pid, signal.SIGTERM)
    if chars:
        if len(chars.encode()) > 32_768:
            raise ValueError('stdin chunks must be at most 32768 bytes')
        session.process.stdin.write(chars.encode())
        session.process.stdin.flush()
    if close_stdin:
        session.process.stdin.close()
    return _result(session_id, session, yield_time_ms)


@atexit.register
def cleanup() -> None:
    for session in list(sessions.values()):
        if session.process.poll() is None:
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
