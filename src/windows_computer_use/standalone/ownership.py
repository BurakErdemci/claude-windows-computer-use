"""Lazy ownership of the session lock and the stop hotkey.

Both are system-wide and exclusive, so a server that merely started must not hold them:
they are taken on the first tool call and released after a quiet period.
"""
import datetime
import json
import os
import threading
import time

from . import config
from .hotkey import HaltHotkey


class SessionLock:
    def __init__(self):
        self.handle = None

    def acquire(self):
        import msvcrt
        handle = None
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            handle = (config.STATE_DIR / "server.lock").open("a+b")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if handle is not None:
                handle.close()
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(moment):
    return moment.isoformat(timespec="seconds")


class Ownership:
    def __init__(self, lock_factory=SessionLock, hotkey_factory=HaltHotkey, clock=time.monotonic,
                 idle_seconds=60, owner_path=None):
        self._lock_factory = lock_factory
        self._hotkey_factory = hotkey_factory
        self._clock = clock
        self.idle_seconds = idle_seconds
        self._owner_path = owner_path if owner_path is not None else config.STATE_DIR / "server.owner.json"
        self._state = threading.Lock()
        self._lock = None
        self._hotkey = None
        self._in_flight = 0
        self._last_done = 0.0
        self._acquired_at = None
        self._halted_at = None
        self._stop = threading.Event()
        self._thread = None

    @property
    def halted_at(self):
        with self._state:
            return self._halted_at_locked()

    @property
    def registered(self):
        with self._state:
            # A released, idle server has no hotkey to report on.
            return self._hotkey.registered if self._lock is not None else True

    def _halted_at_locked(self):
        # The press is remembered here so that it outlives the hotkey that saw it.
        if self._halted_at is None and self._hotkey is not None:
            self._halted_at = self._hotkey.halted_at
        return self._halted_at

    def acquire(self):
        with self._state:
            if self._lock is not None:
                self._in_flight += 1
                return None
            lock = self._lock_factory()
            if not lock.acquire():
                lock.close()
                return self._busy_message()
            hotkey = self._hotkey_factory()
            hotkey.start()
            if not hotkey.registered:
                hotkey.stop()
                lock.close()
                return "The stop hotkey (Ctrl+Alt+Esc) could not be registered; computer use was not started."
            self._lock, self._hotkey = lock, hotkey
            self._acquired_at = _stamp(_now())
            self._write_owner()
            self._in_flight += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._watch_idle, daemon=True)
                self._thread.start()
            return None

    def done(self):
        with self._state:
            self._in_flight = max(0, self._in_flight - 1)
            self._last_done = self._clock()
            if self._lock is not None:
                self._write_owner()

    def release(self):
        with self._state:
            self._release_locked()

    def close(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.release()

    def _release_locked(self):
        if self._lock is None:
            return
        self._halted_at_locked()
        self._hotkey.stop()
        self._hotkey = None
        self._lock.close()
        self._lock = None
        try:
            if json.loads(self._owner_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                self._owner_path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    def _watch_idle(self):
        while not self._stop.wait(1):
            with self._state:
                if (self._lock is not None and self._in_flight == 0
                        and self._clock() - self._last_done >= self.idle_seconds):
                    self._release_locked()

    def _write_owner(self):
        record = {"pid": os.getpid(), "parent_pid": os.getppid(), "cwd": os.getcwd(),
                  "acquired_at": self._acquired_at, "last_used_at": _stamp(_now())}
        temp = self._owner_path.with_name(self._owner_path.name + ".tmp")
        try:
            self._owner_path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(record), encoding="utf-8")
            os.replace(temp, self._owner_path)
        except OSError:
            pass

    def _busy_message(self):
        tail = (f"It is released {self.idle_seconds} s after its last action; "
                "try again then, or close that session.")
        try:
            owner = json.loads(self._owner_path.read_text(encoding="utf-8"))
            cwd = owner["cwd"]
            last = datetime.datetime.fromisoformat(owner["last_used_at"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            seconds = max(0, int((_now() - last).total_seconds()))
            if not isinstance(cwd, str) or not isinstance(owner["pid"], int):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return f"Another Claude Code session is using computer use. {tail}"
        return (f"Another Claude Code session is using computer use (folder: {cwd}, "
                f"last action {seconds} s ago). {tail}")
