"""agentd command line: start | stop | status | rotate-token | version.

`agentd start` daemonizes by default (double fork); --foreground keeps it
in the current process for debugging, containers, and launchd/systemd.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import sys
import time

from agentd import auth, config, server


def _add_common(parser):
    parser.add_argument("--config", default=None, help="config file path")
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--sessions-dir", default=None)
    parser.add_argument("--audit-log", default=None)
    parser.add_argument("--pid-file", default=None)


def _apply_cli_overrides(cfg, args):
    mapping = {"bind": "bind", "port": "port", "token_file": "token-file",
               "sessions_dir": "sessions-dir", "audit_log": "audit-log",
               "pid_file": "pid-file"}
    for key, flag in mapping.items():
        value = getattr(args, flag.replace("-", "_"), None)
        if value is not None:
            cfg[key] = config._coerce(key, value)
    return config._expand_paths(cfg)


def _pid_from_file(pid_file):
    try:
        with open(pid_file) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    # Reap first if it is our own child: os.kill(pid, 0) reports a zombie
    # as alive, which would make stop/status lie about a dead daemon.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass  # not our child; fall through to signal 0
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _wait_for_exit(pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _daemonize():
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "rb") as fh:
        os.dup2(fh.fileno(), 0)
    with open(os.devnull, "ab") as fh:
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)


def _install_term_handler():
    """Make SIGTERM shut down cleanly so finally blocks (pidfile cleanup) run."""
    def _handle_term(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handle_term)


def cmd_start(args):
    cfg = _apply_cli_overrides(config.load_config(args.config), args)
    config.ensure_dirs(cfg)
    pid_file = cfg["pid_file"]
    old_pid = _pid_from_file(pid_file)
    if old_pid and _pid_alive(old_pid):
        print(f"agentd already running (pid {old_pid})", file=sys.stderr)
        return 1
    _install_term_handler()
    if not args.foreground:
        _daemonize()
    # Token is generated here so install.sh / first start can show it once.
    auth.get_token(cfg)
    srv = server.create_server(cfg)
    with open(pid_file, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        srv.serve_forever()
    finally:
        try:
            os.unlink(pid_file)
        except OSError:
            pass
    return 0


def cmd_stop(args):
    cfg = _apply_cli_overrides(config.load_config(args.config), args)
    pid = _pid_from_file(cfg["pid_file"])
    if not pid or not _pid_alive(pid):
        print("agentd is not running", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    if _wait_for_exit(pid, 10):
        print("agentd stopped")
        return 0
    print(f"pid {pid} did not stop; try kill -9", file=sys.stderr)
    return 1


def cmd_status(args):
    cfg = _apply_cli_overrides(config.load_config(args.config), args)
    pid = _pid_from_file(cfg["pid_file"])
    if pid and _pid_alive(pid):
        print(json.dumps({"running": True, "pid": pid,
                          "bind": cfg["bind"], "port": cfg["port"]}))
        return 0
    print(json.dumps({"running": False}))
    return 1


def cmd_rotate_token(args):
    cfg = _apply_cli_overrides(config.load_config(args.config), args)
    config.ensure_dirs(cfg)
    token = auth.rotate_token(cfg)
    # Printed once: the operator copies it into the client config, then it
    # lives only in the 0600 token file.
    print(token)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentd",
        description="Local daemon exposing your own agent CLIs over a "
                    "loopback-only HTTP API.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="start the daemon")
    p_start.add_argument("--foreground", action="store_true",
                         help="do not daemonize")
    _add_common(p_start)
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="stop the daemon")
    _add_common(p_stop)
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="show daemon status")
    _add_common(p_status)
    p_status.set_defaults(func=cmd_status)

    p_rot = sub.add_parser("rotate-token", help="generate a new bearer token")
    _add_common(p_rot)
    p_rot.set_defaults(func=cmd_rotate_token)

    p_ver = sub.add_parser("version", help="print version")
    p_ver.set_defaults(func=lambda a: (print(server.VERSION), 0)[1])
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
