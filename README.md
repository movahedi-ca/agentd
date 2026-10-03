# agentd

A small local daemon that lets your own applications drive the AI coding agents you already pay for. It exposes your installed `claude` and `codex` CLIs over a loopback-only HTTP API, so an app can spawn a session, send prompts, and stream answers without re-hosting a model or buying another subscription.

Python 3.11+, standard library only. No dependencies, no telemetry, no network egress except the agent CLIs themselves.

## Why this exists

If you build an app on top of an AI agent, you usually pick between two bad options. Ship it as an MCP server: cheap for users, but you lose control of context and every new run starts cold. Or own the whole agent: full control, but your users pay for a second subscription to your hosted model.

agentd takes a third path. It runs on the user's own machine, holds sessions against the CLIs they already installed and authenticated, and exposes those sessions over `http://127.0.0.1:8765`. The app owns the agent loop. The user keeps their subscription. Nothing about this is hosted, proxied, or metered by anyone else.

## Quickstart

```sh
git clone https://github.com/movahedi-ca/agentd.git
cd agentd
./install.sh
```

`install.sh` copies the daemon to `~/.local/share/agentd`, puts `agentd` on your `~/.local/bin`, and prints a bearer token once. Then:

```sh
agentd start

TOKEN=$(cat ~/.config/agentd/token)

# create a session against your Claude Code CLI
curl -s -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"claude"}' \
  http://127.0.0.1:8765/v1/sessions
# -> {"id":"92f7dc...","provider":"claude",...}

# send a prompt (blocking)
curl -s -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"summarize the git log in three bullets"}' \
  http://127.0.0.1:8765/v1/sessions/92f7dc.../prompt

# or stream the answer as server-sent events
curl -N -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"summarize the git log in three bullets"}' \
  http://127.0.0.1:8765/v1/sessions/92f7dc.../stream

agentd stop
```

You need the `claude` or `codex` CLI installed and signed in first. agentd shells out to them exactly as you would; if the CLI works in your terminal, it works here.

## Security model

agentd is built for one threat model: it runs on your machine, and it must never become a remote-access tool for your agent subscriptions.

- **Loopback only.** The server binds `127.0.0.1` and refuses anything else at startup. There is no flag to change this; if you need remote access, put your own authenticated reverse proxy in front and accept the risk yourself.
- **Bearer token.** Generated on first run with `secrets.token_urlsafe(32)`, stored at `~/.config/agentd/token` with `0600` permissions, compared in constant time. Every route except `/v1/health` requires it. `AGENTD_TOKEN` overrides the file for automation. Rotate anytime with `agentd rotate-token`.
- **Sandboxed sessions.** Each session gets its own working dir under `~/.local/share/agentd/sessions/<id>/`. Session ids are restricted to a safe alphabet, so one session can never address another's directory.
- **Scrubbed environment.** Agent CLIs run with a minimal allowlist env (`PATH`, `HOME`, `LANG`, provider API keys, a few more). Everything else is dropped, including `AGENTD_TOKEN` itself. Additions go through `extra_env` / `extra_env_allow` in the config file, explicitly.
- **Bounded work.** Per-prompt timeouts (default 300s), a max session lifetime (default 1h, then the session and its dir are deleted), a cap on concurrent sessions (default 8), and a 1MB request body limit.
- **Metadata-only audit log.** `~/.local/share/agentd/audit.log` records method, path, status, session id, and duration as JSON lines. Prompt text and model output are never written there.
- **No phone-home.** The daemon makes zero network connections of its own. The only egress is whatever the agent CLIs do under your subscription.

## API reference

Base: `http://127.0.0.1:8765`. Auth: `Authorization: Bearer <token>` on everything except `GET /v1/health`.

| Method | Path | What it does |
|---|---|---|
| GET | `/v1/health` | `{"status":"ok","version":...,"providers":[...]}`. No auth needed. |
| POST | `/v1/sessions` | Body `{"provider":"claude"}` or `{"provider":"codex"}`. Returns `201` with the session record. |
| GET | `/v1/sessions` | List active sessions. |
| GET | `/v1/sessions/{id}` | One session's record. |
| POST | `/v1/sessions/{id}/prompt` | Body `{"prompt":"...","timeout":120}`. Blocks, returns `{"output":...,"exit_code":...,"duration_ms":...}`. |
| POST | `/v1/sessions/{id}/stream` | Same body. Streams `text/event-stream`: `data: {"chunk": "..."}` events, then `data: {"done": true}`, then the connection closes. A mid-stream timeout emits `data: {"error": "..."}` followed by `{"done": true}`; a CLI that exits with no output ends with just `{"done": true}`. |
| DELETE | `/v1/sessions/{id}` | Ends the session and deletes its working dir. Returns `204`. |

Errors are JSON: `{"error":"unauthorized"}`, `{"error":"no_such_session"}`, `{"error":"unknown_provider"}`, `{"error":"body_too_large"}`, `{"error":"prompt_timeout"}` (504), `{"error":"adapter_error"}` (502), `{"error":"session_limit"}` (429).

## Adapters

An adapter is a small class with three methods: `check_available()`, `run(prompt, ...)`, and `stream(prompt, ...)` (the default streams by running once). Adding a provider means subclassing `agentd.adapters.AgentAdapter` and registering it in `REGISTRY`. Prompts are always passed as argv, never through a shell.

- **claude** (`claude` CLI): runs `claude -p --output-format json`, parses the `result` field, and reuses the returned `session_id` with `--resume` on later prompts in the same session (best effort; falls back cleanly on older CLIs).
- **codex** (`codex` CLI): runs `codex exec`. One shot per prompt; the session's working dir persists between prompts.

Both run inside the session's working dir with the scrubbed env described above.

## Configuration

`~/.config/agentd/config.toml` (all optional):

```toml
port = 8765
max_sessions = 8
session_ttl_seconds = 3600
prompt_timeout_seconds = 300
max_request_bytes = 1048576
audit_enabled = true

[extra_env]
# FOO = "bar"          # passed through to agent CLIs

# extra_env_allow = ["SOME_VAR"]  # allow this var through the scrubber
```

Any key can also be set as `AGENTD_<KEY>` in the environment, which wins over the file. `bind` exists but only loopback values are accepted; anything else aborts startup.

## FAQ

**Does this violate Claude's or OpenAI's terms?**
agentd shells out to the official CLIs under your own login and subscription, the same way a shell script would. Whether a particular use is allowed is between you and your provider's terms; check them before wiring this into anything commercial.

**Why not just build an MCP server?**
MCP is the right call when you want any agent to consume your capability. agentd is for the opposite direction: your app wants to drive a specific agent the user already runs. The two compose fine.

**Does it store my prompts?**
No. Prompts go to the CLI's stdin-equivalent and the answer comes back over the API. The audit log keeps metadata only. The CLIs themselves may keep their own history per their normal behavior.

**Windows?**
Untested. The code avoids platform-specific calls where it can, but path handling and process control were built on Linux/macOS. Reports and patches welcome.

## Development

```sh
python3 -m unittest discover -s tests   # 68 tests, stdlib only
```

The suite covers auth rejection, loopback-only binding, sandbox isolation, adapter behavior against fake CLIs, session lifecycle and TTL, timeouts, and the full HTTP surface including SSE. No commit lands unless the suite is green on the exact tree being committed.
