# Security Policy

Odysseus is a self-hosted AI workspace with privileged local capabilities (a shell, a Python runtime, file access, email, model serving, and a secret vault). It is designed to run on a machine you control. Please do not run it as a public, unauthenticated service.

This document describes the threat model and the secure-by-default protections, the configuration knobs that control them, and the honest residual risks.

## Supported Versions

Security fixes are handled on the default branch until formal releases are cut.

## Threat Model

The agent acts on your behalf with real local power, so the main risks are:

1. **Prompt injection.** The agent reads untrusted content (emails, fetched pages, documents). That content can try to steer the agent into taking actions you did not ask for.
2. **Privilege misuse.** Tools such as the shell, Python, file write, email send, settings, tokens, and model serving can damage the host, exfiltrate data, or change configuration.
3. **Secret leakage.** Provider API keys, app tokens, and vault contents can leak into model context (via tool output) or into logs.
4. **Network exposure.** An instance bound to a non-loopback interface without authentication exposes all of the above to the network.

## Secure-By-Default Protections

These are on by default. They reduce blast radius but do not replace good deployment hygiene.

### 1. Default-deny capability model

Every tool is tiered in a registry (`src/tool_security.py`): `read_only`, `stateful` (the user's own scope), or `privileged` (shell, Python, file access, email, serving, settings, secrets). Privileged tools are admin-only. A tool added without a policy, and any `mcp__*` tool, defaults to `privileged` (denied to non-admins), so a new capability is closed, not exposed, until it is explicitly tiered.

### 2. Sandboxed command execution (on by default)

The agent's `bash` and `python` tools run inside a sandbox in a per-session workspace, not directly on the host:

- **Linux:** a `bubblewrap` namespace sandbox (filesystem, network, and process isolation).
- **macOS / Windows:** a `pathjail` fallback (clean working directory, scrubbed environment, resource and time limits). This is deliberately weaker (see residual risks).
- `SANDBOX_BACKEND=auto` (the default) selects the strongest available backend and degrades from bubblewrap to pathjail when bubblewrap is unavailable. It never degrades to no isolation. Naming an explicit backend (for example `SANDBOX_BACKEND=bubblewrap`) fails closed if that backend is unavailable.
- Set `SANDBOX_BACKEND=none` only for dev-only direct-host execution.
- Network access and host directory mounts are off by default and are granted only explicitly via `SANDBOX_ALLOW_NETWORK=1` and `SANDBOX_MOUNTS=host:target[:ro|rw],...`.

### 3. Prompt-injection quarantine (opt-in)

For high-risk autonomous paths, untrusted text can be reduced to a schema-validated structure by a tool-less model call (`src/quarantine.py`), so an injected instruction inside the content cannot reach a tool-capable step. Enable with `quarantine_enabled` in settings. It is currently wired to the email-triage classifier and is off by default.

### 4. Authentication and the unconfigured fail-open

When authentication is configured, privileged tools require an admin. On a fresh, unconfigured instance there is a single-user convenience that treats the local user as an admin, but this is granted to loopback callers only. Remote callers on an unconfigured instance are denied at three layers: the auth middleware returns 401 before any tool runs, the capability layer fails closed for remote origins, and regression tests pin both behaviors.

### 5. Secret redaction

Known secrets are masked before tool output re-enters model context and before log lines are emitted (`src/redaction.py`): provider API keys, app tokens, GitHub and AWS keys, bearer tokens, and PEM private key blocks, plus environment values whose variable name marks them as secret.

## Configuration Knobs

| Variable / setting | Default | Effect |
| --- | --- | --- |
| `AUTH_ENABLED` | `true` | Enables the authentication middleware. Keep it on. |
| `LOCALHOST_BYPASS` | `false` | Lets trusted loopback callers skip auth. Keep it off on any network-exposed deployment. |
| `SANDBOX_BACKEND` | `auto` | `auto` (strongest available, degrades to pathjail), `bubblewrap`, `pathjail`, `docker`, or `none` (dev-only direct host). |
| `SANDBOX_ALLOW_NETWORK` | off | Grants the sandbox network access. Off by default. |
| `SANDBOX_MOUNTS` | empty | Explicit host directory mounts for the sandbox (`host:target[:ro|rw]`). Empty by default. |
| `quarantine_enabled` (setting) | `false` | Routes untrusted email-triage text through the injection quarantine. |

A loud startup warning is logged when `AUTH_ENABLED=false`, `LOCALHOST_BYPASS=true`, or no users are configured yet, so a risky posture is visible at boot.

## Residual Risks (Honest Limits)

- **pathjail is weak.** On macOS and Windows the fallback is not a real filesystem or network boundary. For strong isolation on those platforms, run under Docker, or run on Linux with bubblewrap.
- **Redaction is heuristic.** Pattern matching can miss a bespoke token shape, and value-based redaction currently covers secrets present in the environment by a secret-looking name. Treat it as a safety net, not a guarantee. Still rotate any secret that appears in logs, screenshots, or shared chats.
- **Quarantine is scoped.** It is opt-in and wired to a subset of paths. The autonomous email paths are already tool-less, so quarantine there is defense in depth rather than the only line of defense.
- **Bind host is a deployment concern.** The process cannot reliably self-detect its bind interface (it is a launch argument), so binding to loopback or putting the app behind a trusted reverse proxy remains your responsibility.

## Deployment Guidance

- Keep `AUTH_ENABLED=true` and `LOCALHOST_BYPASS=false` on anything beyond your own loopback.
- Use HTTPS and a trusted reverse proxy or private network when exposing the app beyond localhost.
- Protect `.env`, `data/`, logs, uploaded files, generated media, and database files.
- Disable open signup unless you intentionally want new accounts.
- Keep demo and test users non-admin, and remove them entirely on serious deployments.
- Give admin accounts strong passwords and enable 2FA where possible.
- Leave high-risk agent tools (shell, Python, file read and write, email, MCP, app API, task, skill, memory, settings, tokens, model serving, vault) restricted to admins.
- Rotate API keys, webhook secrets, and Odysseus API tokens if they appear in logs, screenshots, demos, or shared chats.

## Publishing A Fork

Before pushing a public fork, run:

```bash
git status --short
git check-ignore -v .env data/auth.json data/app.db logs/compound.log odysseus.db
git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{20,}|Bearer [A-Za-z0-9._~+/-]{20,})" -- . ':!static/lib/**' ':!package-lock.json'
```

Only `.env.example`, docs, source, tests, and static assets should be committed. Never commit live `data/` contents, local databases, uploaded files, generated media, logs, backups, API keys, password hashes, or personal documents.

## Reporting

Please report vulnerabilities privately via GitHub security advisories if available, or by opening a minimal issue that does not disclose exploit details.
