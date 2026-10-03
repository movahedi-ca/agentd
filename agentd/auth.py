"""Bearer-token auth. Token lives in a 0600 file; AGENTD_TOKEN env overrides it.

Comparisons use hmac.compare_digest (constant time). Empty tokens never
verify, so a missing token cannot accidentally authorize.
"""
from __future__ import annotations

import hmac
import os
import secrets

TOKEN_ENV_VAR = "AGENTD_TOKEN"


def generate_token():
    return secrets.token_urlsafe(32)


def _write_token_file(path):
    """Create the token file with 0600 perms, atomically where possible."""
    token = generate_token()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
    except BaseException:
        os.close(fd)
        raise
    os.chmod(path, 0o600)  # fix perms if the file already existed
    return token


def get_token(cfg):
    """Return the active bearer token, generating and storing one if needed."""
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token
    path = cfg["token_file"]
    if os.path.exists(path):
        os.chmod(path, 0o600)
        with open(path) as fh:
            return fh.read().strip()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return _write_token_file(path)


def rotate_token(cfg):
    """Replace the stored token and return the new value. Prints nothing."""
    if TOKEN_ENV_VAR in os.environ:
        raise RuntimeError(
            f"cannot rotate: token is currently supplied via {TOKEN_ENV_VAR}"
        )
    path = cfg["token_file"]
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return _write_token_file(path)


def verify_token(provided, expected):
    """Constant-time bearer comparison. Empty values never verify."""
    if not provided or not expected:
        return False
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(provided, expected)
