"""Session lifecycle: create, prompt, stream, list, delete, TTL expiry.

Sessions are keyed by random hex ids. Each session owns an isolated working
dir (see agentd.sandbox) and one adapter instance. Prompts are serialized
per session with a lock. A wall-clock timeout bounds every prompt even if
an adapter ignores its own timeout: the run happens on a daemon thread and
PromptTimeoutError is raised to the caller.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid

from agentd import adapters, sandbox


class SessionNotFoundError(Exception):
    pass


class SessionLimitError(Exception):
    pass


class SessionExpiredError(SessionNotFoundError):
    pass


class PromptTimeoutError(Exception):
    pass


class Session:
    def __init__(self, session_id, provider, adapter, workdir):
        self.id = session_id
        self.provider = provider
        self.adapter = adapter
        self.dir = workdir
        self.created_at = time.time()
        self.last_active = self.created_at
        self.prompts = 0
        self.lock = threading.Lock()

    def expired(self, ttl_seconds):
        return (time.time() - self.created_at) > ttl_seconds

    def to_dict(self):
        return {
            "id": self.id,
            "provider": self.provider,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "prompts": self.prompts,
        }


def _run_with_timeout(func, timeout):
    """Run func() on a daemon thread; raise PromptTimeoutError on timeout."""
    out = queue.Queue(maxsize=1)

    def target():
        try:
            out.put(("ok", func()))
        except BaseException as exc:  # noqa: BLE001 - re-raised to caller
            out.put(("err", exc))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        status, payload = out.get(timeout=timeout)
    except queue.Empty:
        raise PromptTimeoutError(f"prompt exceeded {timeout}s")
    if status == "err":
        raise payload
    return payload


class SessionManager:
    def __init__(self, cfg, adapter_factories=None):
        self.cfg = cfg
        self.factories = adapter_factories or dict(adapters.REGISTRY)
        self._sessions = {}
        self._guard = threading.Lock()

    def _sweep(self):
        ttl = self.cfg["session_ttl_seconds"]
        expired = [sid for sid, s in self._sessions.items() if s.expired(ttl)]
        for sid in expired:
            self._remove(sid)

    def _remove(self, session_id):
        session = self._sessions.pop(session_id, None)
        if session is not None:
            sandbox.remove_session_dir(self.cfg["sessions_dir"], session_id)

    def create(self, provider):
        with self._guard:
            self._sweep()
            if provider not in self.factories:
                raise adapters.UnknownProviderError(
                    f"unknown provider {provider!r}; "
                    f"available: {sorted(self.factories)}")
            if len(self._sessions) >= self.cfg["max_sessions"]:
                raise SessionLimitError(
                    f"max sessions reached ({self.cfg['max_sessions']})")
            session_id = uuid.uuid4().hex
            workdir = sandbox.make_session_dir(self.cfg["sessions_dir"], session_id)
            factory = self.factories[provider]
            adapter = factory() if isinstance(factory, type) else factory
            session = Session(session_id, provider, adapter, workdir)
            self._sessions[session_id] = session
            return session

    def get(self, session_id):
        with self._guard:
            self._sweep()
            try:
                return self._sessions[session_id]
            except KeyError:
                raise SessionNotFoundError(f"no such session: {session_id!r}")

    def list(self):
        with self._guard:
            self._sweep()
            return [s.to_dict() for s in self._sessions.values()]

    def delete(self, session_id):
        with self._guard:
            self._sweep()
            if session_id not in self._sessions:
                raise SessionNotFoundError(f"no such session: {session_id!r}")
            self._remove(session_id)

    def prompt(self, session_id, prompt, timeout=None):
        session = self.get(session_id)
        timeout = timeout if timeout is not None else self.cfg["prompt_timeout_seconds"]
        env = sandbox.scrub_env(cfg=self.cfg)
        with session.lock:
            try:
                result = _run_with_timeout(
                    lambda: session.adapter.run(
                        prompt, cwd=session.dir, timeout=timeout, env=env),
                    timeout + 5,
                )
            except adapters.AdapterTimeout as exc:
                raise PromptTimeoutError(str(exc)) from exc
            session.prompts += 1
            session.last_active = time.time()
            return result

    def stream(self, session_id, prompt, timeout=None):
        """Yield output chunks for a prompt (SSE source). Blocking iterator.

        A wall-clock deadline bounds the whole stream: the adapter's
        generator is pumped on a daemon thread and each chunk must arrive
        before the deadline, so even an adapter that ignores its own
        timeout cannot hang the caller past it.
        """
        session = self.get(session_id)
        timeout = timeout if timeout is not None else self.cfg["prompt_timeout_seconds"]
        env = sandbox.scrub_env(cfg=self.cfg)
        deadline = time.monotonic() + timeout + 5
        with session.lock:
            gen = session.adapter.stream(
                prompt, cwd=session.dir, timeout=timeout, env=env)
            pump_queue = queue.Queue()

            def _pump():
                try:
                    for chunk in gen:
                        pump_queue.put(("chunk", chunk))
                except BaseException as exc:  # noqa: BLE001 - forwarded
                    pump_queue.put(("error", exc))
                finally:
                    pump_queue.put(("end", None))

            pump = threading.Thread(target=_pump, daemon=True)
            pump.start()
            yielded_any = False
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PromptTimeoutError(
                            f"stream exceeded {timeout}s")
                    try:
                        kind, payload = pump_queue.get(timeout=remaining)
                    except queue.Empty:
                        raise PromptTimeoutError(
                            f"stream exceeded {timeout}s")
                    if kind == "end":
                        break
                    if kind == "error":
                        raise payload
                    session.last_active = time.time()
                    yielded_any = True
                    yield payload
            except adapters.AdapterTimeout as exc:
                raise PromptTimeoutError(str(exc)) from exc
            if yielded_any:
                session.prompts += 1
            session.last_active = time.time()
