"""Sandboxing: isolated per-session working dirs and a scrubbed child env.

Session ids are constrained to a safe alphabet so a session id can never
escape the sessions base directory. Child processes for agent CLIs get a
minimal allowlist env: enough for the CLIs to authenticate (HOME for
credential files, provider API keys) and nothing else.
"""
from __future__ import annotations

import os
import re
import shutil

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Env vars passed through to agent CLIs. Provider keys and HOME (where the
# CLIs keep OAuth credential files) are required for the user's own
# subscription to work. Everything else is dropped.
ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
})

# Never pass these through, even if allowlisted by name elsewhere.
ENV_DENYLIST = frozenset({"AGENTD_TOKEN"})


class SandboxError(Exception):
    pass


def _check_session_id(session_id):
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise SandboxError(f"invalid session id: {session_id!r}")


def make_session_dir(base_dir, session_id):
    """Create and return the isolated working dir for a session."""
    _check_session_id(session_id)
    path = os.path.realpath(os.path.join(base_dir, session_id))
    base = os.path.realpath(base_dir)
    if path != os.path.join(base, session_id):
        raise SandboxError(f"session dir escapes base: {session_id!r}")
    os.makedirs(path, exist_ok=True)
    return path


def remove_session_dir(base_dir, session_id):
    """Delete a session's working dir. Refuses anything outside base_dir."""
    _check_session_id(session_id)
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, session_id))
    if os.path.dirname(path) != base:
        raise SandboxError(f"session dir escapes base: {session_id!r}")
    shutil.rmtree(path, ignore_errors=True)


def scrub_env(source_env=None, cfg=None):
    """Return a minimal env for agent CLI child processes.

    Keeps the allowlist, drops everything else (including AGENTD_TOKEN),
    then applies cfg['extra_env'] overrides and cfg['extra_env_allow']
    pass-throughs.
    """
    cfg = cfg or {}
    source = os.environ if source_env is None else source_env
    extra_allow = set(cfg.get("extra_env_allow") or [])
    out = {}
    for name, value in source.items():
        if name in ENV_DENYLIST:
            continue
        if name in ENV_ALLOWLIST or name in extra_allow:
            out[name] = value
    for name, value in (cfg.get("extra_env") or {}).items():
        out[str(name)] = str(value)
    return out
