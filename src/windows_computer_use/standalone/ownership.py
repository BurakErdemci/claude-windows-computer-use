"""Lazy ownership of the session lock and the stop hotkey.

Both are system-wide and exclusive, so a server that merely started must not hold them:
they are taken on the first tool call and released after a quiet period.
"""
import datetime
import errno
import json
import os
import threading
import time

from . import config
from .hotkey import HaltHotkey


class StateDirError(OSError):
    """The state folder cannot be created or the lock file cannot be opened."""

    def __init__(self, path):
        super().__init__(str(path))
        self.path = path


class SessionLock:
    def __init__(self):
        self.handle = None

    def acquire(self):
        """True when taken, False when another process holds it; StateDirError when the folder is unusable."""
        import msvcrt
        try:
            config.STATE_DIR.mkdir(parents=True, exist_ok=True)
            handle = (config.STATE_DIR / "server.lock").open("a+b")
        except OSError as exc:
            raise StateDirError(config.STATE_DIR) from exc
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            # Windows reports a byte range held elsewhere as EACCES (or EDEADLK); anything else is not contention.
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise StateDirError(config.STATE_DIR / "server.lock") from exc
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
        self._halt_written = False
        self._retired = []
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
        if self._halted_at is None:
            # Retired hotkeys count too: a press can be processed after stop() gave up joining its thread.
            for hotkey in ([self._hotkey] if self._hotkey is not None else []) + self._retired:
                if hotkey.halted_at is not None:
                    self._halted_at = hotkey.halted_at
                    break
        if self._halted_at is not None and not self._halt_written and self._lock is not None:
            self._halt_written = True
            self._write_owner()
        return self._halted_at

    def acquire(self):
        with self._state:
            if self._lock is not None:
                self._in_flight += 1
                return None
            lock = self._lock_factory()
            try:
                taken = lock.acquire()
            except StateDirError as exc:
                lock.close()
                return (f"Computer use cannot use its state folder ({exc.path}); "
                        "check that it exists and is writable.")
            if not taken:
                lock.close()
                return self._busy_message()
            hotkey = None
            try:
                hotkey = self._hotkey_factory()
                hotkey.start()
                registered = hotkey.registered
            except Exception:
                registered = False
            if not registered:
                if hotkey is not None:
                    try:
                        hotkey.stop()
                    except Exception:
                        pass
                lock.close()
                return "The stop hotkey (Ctrl+Alt+Esc) could not be registered; computer use was not started."
            self._lock, self._hotkey = lock, hotkey
            self._acquired_at = _stamp(_now())
            self._halt_written = self._halted_at is not None
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
        with self._state:
            self._release_locked(force=True)

    def _release_locked(self, force=False):
        if self._lock is None:
            return
        # stop() joins the hotkey thread, so a press that raced the release is visible afterwards.
        hotkey, self._hotkey = self._hotkey, None
        hotkey.stop()
        if hotkey not in self._retired:
            self._retired.append(hotkey)
        if self._halted_at is None:
            self._halted_at = hotkey.halted_at
        if self._halted_at is not None and not force:
            # A halted server keeps the lock until it exits, even when the halt only showed up now.
            self._hotkey = hotkey
            self._halt_written = True
            self._write_owner()
            return
        # The owner file goes while the lock is still held: once it is free, the next holder writes its own.
        try:
            if json.loads(self._owner_path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                self._owner_path.unlink()
        except (OSError, ValueError, AttributeError):
            pass
        self._lock.close()
        self._lock = None

    def _watch_idle(self):
        while not self._stop.wait(1):
            with self._state:
                # A halted server keeps the lock until it exits, so no session resumes without a restart.
                if (self._lock is not None and self._in_flight == 0 and self._halted_at_locked() is None
                        and self._clock() - self._last_done >= self.idle_seconds):
                    self._release_locked()

    def _write_owner(self):
        record = {"pid": os.getpid(), "parent_pid": os.getppid(), "cwd": os.getcwd(),
                  "acquired_at": self._acquired_at, "last_used_at": _stamp(_now())}
        if self._halted_at is not None:
            record["halted"] = True
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
            if owner.get("halted") is True:
                return ("Another Claude Code session holds computer use and was stopped with Ctrl+Alt+Esc "
                        f"(folder: {cwd}). It stays locked until that session restarts its computer-use "
                        "server (/mcp) or closes.")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return f"Another Claude Code session is using computer use. {tail}"
        return (f"Another Claude Code session is using computer use (folder: {cwd}, "
                f"last action {seconds} s ago). {tail}")
