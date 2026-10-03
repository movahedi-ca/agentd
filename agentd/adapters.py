"""Provider adapters: shell out to the user's own agent CLIs, non-interactively.

agentd never re-hosts a model and never proxies credentials. Each adapter
runs the official CLI the user already installed and authenticated (their
own subscription), inside the session's sandboxed working dir, with a
scrubbed environment and a hard timeout. The prompt is always passed as an
argv element, never through a shell.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AdapterError(Exception):
    pass


class AdapterTimeout(AdapterError):
    pass


class UnknownProviderError(AdapterError):
    pass


@dataclass
class AdapterResult:
    output: str
    exit_code: int
    duration_s: float
    # Opaque conversation handle the CLI returned (e.g. Claude session id),
    # used for best-effort resume on the next prompt. May be None.
    session_ref: str | None = None
    truncated: bool = False


class AgentAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def check_available(self) -> bool:
        """True if the CLI binary is on PATH."""

    @abstractmethod
    def run(self, prompt, *, cwd, timeout, env) -> AdapterResult:
        """Run one non-interactive prompt. Raises AdapterTimeout on timeout."""

    def stream(self, prompt, *, cwd, timeout, env):
        """Yield output chunks. Default: run once and yield the full output."""
        yield self.run(prompt, cwd=cwd, timeout=timeout, env=env).output


def _run_cli(argv, *, cwd, timeout, env):
    """Run a CLI with no shell, capturing output, enforcing timeout."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as exc:
        raise AdapterError(f"CLI not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterTimeout(
            f"CLI timed out after {timeout}s: {argv[0]}") from exc
    return proc, time.monotonic() - started


class ClaudeAdapter(AgentAdapter):
    """Adapter for the Claude Code CLI (`claude`)."""

    name = "claude"

    def __init__(self):
        self._resume_ref = None

    def check_available(self):
        return shutil.which("claude") is not None

    def _argv(self, prompt):
        argv = ["claude", "-p", "--output-format", "json"]
        if self._resume_ref:
            argv += ["--resume", self._resume_ref]
        return argv + [prompt]

    def run(self, prompt, *, cwd, timeout, env):
        if not self.check_available():
            raise AdapterError("claude CLI not found on PATH")
        argv = self._argv(prompt)
        try:
            proc, duration = _run_cli(argv, cwd=cwd, timeout=timeout, env=env)
        except AdapterTimeout:
            self._resume_ref = None
            raise
        output, session_ref = self._parse(proc.stdout)
        if self._resume_ref and proc.returncode != 0 and b"resume" in proc.stderr.lower().encode():
            # Older CLI without --resume: retry once without it.
            proc, duration = _run_cli(
                ["claude", "-p", "--output-format", "json", prompt],
                cwd=cwd, timeout=timeout, env=env)
            output, session_ref = self._parse(proc.stdout)
        if session_ref:
            self._resume_ref = session_ref
        if proc.returncode != 0 and not output:
            raise AdapterError(
                f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        return AdapterResult(output=output, exit_code=proc.returncode,
                             duration_s=duration, session_ref=session_ref)

    @staticmethod
    def _parse(stdout):
        """Extract (result text, session_id) from `claude -p --output-format json`."""
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            return stdout.strip(), None
        if isinstance(payload, dict):
            return (str(payload.get("result", "")).strip(),
                    payload.get("session_id"))
        return stdout.strip(), None

    def stream(self, prompt, *, cwd, timeout, env):
        # Claude's JSON envelope does not stream cleanly, so stream plain
        # text output line by line from a non-JSON invocation.
        if not self.check_available():
            raise AdapterError("claude CLI not found on PATH")
        argv = ["claude", "-p"]
        if self._resume_ref:
            argv += ["--resume", self._resume_ref]
        argv.append(prompt)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as exc:
            raise AdapterError("claude CLI not found on PATH") from exc
        try:
            for line in proc.stdout:
                yield line
            remaining = max(1.0, timeout - (time.monotonic() - started))
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise AdapterTimeout(f"claude stream timed out after {timeout}s") from exc
        finally:
            if proc.poll() is None:
                proc.kill()


class CodexAdapter(AgentAdapter):
    """Adapter for the Codex CLI (`codex exec`). Single-shot per prompt."""

    name = "codex"

    def check_available(self):
        return shutil.which("codex") is not None

    def run(self, prompt, *, cwd, timeout, env):
        if not self.check_available():
            raise AdapterError("codex CLI not found on PATH")
        proc, duration = _run_cli(["codex", "exec", prompt],
                                  cwd=cwd, timeout=timeout, env=env)
        output = proc.stdout.strip()
        if proc.returncode != 0 and not output:
            raise AdapterError(
                f"codex exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        return AdapterResult(output=output, exit_code=proc.returncode,
                             duration_s=duration)


REGISTRY = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}


def get_adapter(name):
    try:
        factory = REGISTRY[name]
    except KeyError:
        raise UnknownProviderError(
            f"unknown provider {name!r}; available: {sorted(REGISTRY)}")
    return factory()


def available_providers():
    return [{"name": name, "available": get_adapter(name).check_available()}
            for name in sorted(REGISTRY)]
