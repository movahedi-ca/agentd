"""Configuration: defaults, TOML file, AGENTD_* env overrides, directory layout.

Only loopback binds are ever honored; anything else is rejected at server
startup (see agentd.server). The config layer just carries the value.
"""
from __future__ import annotations

import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11, not expected but be explicit
    tomllib = None  # type: ignore[assignment]

DEFAULTS = {
    "bind": "127.0.0.1",
    "port": 8765,
    "token_file": "~/.config/agentd/token",
    "sessions_dir": "~/.local/share/agentd/sessions",
    "audit_log": "~/.local/share/agentd/audit.log",
    "pid_file": "~/.local/share/agentd/agentd.pid",
    "max_sessions": 8,
    "session_ttl_seconds": 3600,
    "prompt_timeout_seconds": 300,
    "max_request_bytes": 1_048_576,
    "audit_enabled": True,
    # Extra env vars to pass through to agent CLIs, beyond the scrubbed
    # allowlist (see agentd.sandbox). Example: {"HTTP_PROXY": "..."}.
    "extra_env": {},
    # Extra env var *names* to allow through the sandbox scrubber.
    "extra_env_allow": [],
}

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/agentd/config.toml")

_INT_KEYS = {"port", "max_sessions", "session_ttl_seconds",
             "prompt_timeout_seconds", "max_request_bytes"}
_BOOL_KEYS = {"audit_enabled"}
_ENV_PREFIX = "AGENTD_"


class ConfigError(Exception):
    pass


def _coerce(key, value):
    if key in _INT_KEYS:
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"config key {key!r} must be an integer, got {value!r}")
        if key == "port" and not (0 <= ivalue <= 65535):
            raise ConfigError(f"port out of range: {ivalue}")
        if ivalue < 0:
            raise ConfigError(f"config key {key!r} must be >= 0")
        return ivalue
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"config key {key!r} must be boolean, got {value!r}")
    if key in ("extra_env", "extra_env_allow"):
        return value
    return str(value)


def _expand_paths(cfg):
    for key in ("token_file", "sessions_dir", "audit_log", "pid_file"):
        cfg[key] = os.path.abspath(os.path.expanduser(cfg[key]))
    return cfg


def load_config(path=None):
    """Load config: DEFAULTS <- TOML file <- AGENTD_* env vars."""
    cfg = dict(DEFAULTS)
    cfg_path = path or DEFAULT_CONFIG_PATH
    if cfg_path and os.path.exists(cfg_path):
        if tomllib is None:
            raise ConfigError("tomllib unavailable; cannot read config file")
        with open(cfg_path, "rb") as fh:
            try:
                data = tomllib.load(fh)
            except Exception as exc:
                raise ConfigError(f"cannot parse {cfg_path}: {exc}")
        for key, value in data.items():
            if key not in DEFAULTS:
                raise ConfigError(f"unknown config key {key!r} in {cfg_path}")
            cfg[key] = _coerce(key, value)
    for key in DEFAULTS:
        env_name = _ENV_PREFIX + key.upper()
        if env_name in os.environ:
            cfg[key] = _coerce(key, os.environ[env_name])
    if not isinstance(cfg["extra_env"], dict):
        raise ConfigError("extra_env must be a table of name = value")
    if not isinstance(cfg["extra_env_allow"], list):
        raise ConfigError("extra_env_allow must be a list of names")
    return _expand_paths(cfg)


def ensure_dirs(cfg):
    """Create the sessions dir and the parent dirs of token/audit/pid files."""
    os.makedirs(cfg["sessions_dir"], exist_ok=True)
    for key in ("token_file", "audit_log", "pid_file"):
        parent = os.path.dirname(cfg[key])
        if parent:
            os.makedirs(parent, exist_ok=True)
