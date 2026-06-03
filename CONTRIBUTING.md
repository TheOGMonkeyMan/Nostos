# Contributing to Odysseus

Thanks for helping. The project is moving quickly, so the best contributions are focused, easy to review, and easy to test.

## Before You Start

- Search existing issues and pull requests before opening a new one.
- Prefer one bug fix or feature per pull request.
- Avoid broad rewrites, formatting-only changes, or moving many files unless the issue is specifically about structure.
- If you want to work on a large feature, open an issue first and describe the approach.

## Setup

Docker is the recommended path for normal testing:

```bash
git clone https://github.com/pewdiepie-archdaemon/odysseus.git
cd odysseus
cp .env.example .env
docker compose up -d --build
```

Manual development uses Python 3.11+:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 7000
```

CI runs the test suite on **Linux, macOS, and Windows** (Python 3.11 and 3.12), so all three are supported targets. Docker on Linux is still the simplest path for a full-stack run.

### Common tasks (justfile)

With [`just`](https://github.com/casey/just) installed and your venv active:

```bash
just test        # pytest
just lint        # ruff check
just fmt         # ruff format
just typecheck   # mypy (gradual)
just dev         # uvicorn with autoreload on :7000
just smoke       # docker compose up, poll /healthz, down
```

Install the pre-commit hooks once so lint/format/whitespace and the file-length
ratchet run on every commit:

```bash
pip install pre-commit && pre-commit install
```

## Running Checks

Run the smallest relevant checks for your change:

```bash
python -m pytest
python -m py_compile app.py routes/*.py src/*.py
node --check static/js/<file-you-changed>.js
```

For Docker-related changes:

```bash
docker compose config
docker compose up -d --build
docker compose logs --tail=120 odysseus
```

Mention what you ran in the pull request description. If you could not run a check, say so.

## Architecture map

Orientation for new contributors (counts from the baseline audit; see the improvement plan section 1):

- **`app.py`** - the wiring monolith (~1,000 lines): builds the FastAPI app, registers middleware (CORS, security headers, request-timeout, auth) and `include_router`s ~40 route modules. Keep handlers thin; delegate to a service.
- **`routes/`** (~47 files) - HTTP endpoints, one module per feature, wired via `setup_*_routes(...)` factories.
- **`src/`** (~79 files) - core logic: agent loop, tool execution/parsing/registry, LLM core, RAG/memory/vector, research, security helpers.
- **`services/`** (~38 files) - service-layer modules (search, memory, shell, stt/tts, hwfit, research). Partly duplicated with `src/` (see murky corners).
- **`core/`** (~10 files) - database models + session, auth, middleware, atomic IO, health.
- **`mcp_servers/`** (6) - built-in MCP servers: email, image_gen, memory, rag.
- **`tests/`** (~28 files) - regression suite, weighted toward security/auth.
- **`static/`** (~152 files) - vanilla-JS frontend (no framework); a large `style.css` + `app.js`.

What is solid and worth building on: the security-weighted regression suite; real security middleware; `src/prompt_security.py` (data/instruction separation); 2FA + bcrypt + secret store + rate limiter; working IMAP/SMTP, CalDAV, MCP, and deep-research integrations.

## The strangler-fig rule

We improve in place and never stop the running ship to rebuild it. Every change:

- hides behind an interface the rest of the code already uses, **or** migrates all callers in the same PR;
- ships **green through CI** as a small increment (no long-lived rewrite branches);
- has a **rollback** (a config flag or a single revert) stated in the PR description;
- is proven behavior-preserving by tests that pass before and after (write a characterization test first when touching untested code).

Do not decompose, rename, or reformat for its own sake. Refactor a file only when you are already editing it for a functional reason, or during a scheduled debt task.

## Murky corners - touch with tests

These areas are powerful but fragile or mid-migration. Pin current behavior with a test before changing them:

- **`services/shell/service.py`** - runs commands on the host; titled "safe" but currently unsandboxed. Security-critical (sandboxing is planned). Never weaken its guards.
- **`src/prompt_security.py` + tool execution** - the prompt-injection defense is prompt-level only and sits in front of high-risk capabilities (shell, email/web ingestion). Treat all external input as untrusted.
- **`src/tool_security.py`** - capability gating via a denylist with a fail-open when auth is unconfigured. Adding a tool can expose it by default; check the gate.
- **`src/` and `services/` duplication** - near-identical parallel trees (e.g. both have a `search/`). A half-finished migration that drifts; do not edit one copy in isolation.
- **God-files** (listed in `scripts/file_length_baseline.txt`) - `tool_implementations.py`, `email_routes.py`, `builtin_actions.py`, `task_scheduler.py`, `agent_loop.py`, `cookbook_routes.py`, `core/database.py`. They may shrink, not grow.
- **`chromadb-client` + a standalone ChromaDB container** - the most common fresh-machine startup failure; an embedded replacement is planned.
- **`core/database.py`** - builds its engine at import time, so importing it needs a writable `./data` dir.

`tests/test_security_regressions.py` and the auth/sandbox suites are the highest-priority tests. Never weaken them to make a change pass.

## Pull Requests

Good pull requests usually include:

- A short explanation of the bug or feature.
- The files or areas changed.
- Manual test steps or automated test results.
- Screenshots or short recordings for UI changes.
- Links to related issues, for example `Fixes #123`.

Please keep PRs small. Large PRs that mix unrelated cleanup, formatting, refactors, and behavior changes are much harder to review.

## Issue Reports

For bugs, include:

- Install method: Docker, manual Python, WSL, etc.
- OS, browser, and device if relevant.
- Exact steps to reproduce.
- Expected behavior and actual behavior.
- Logs, screenshots, or terminal output.

For model-serving issues, include:

- Backend: Ollama, vLLM, SGLang, llama.cpp, LM Studio, etc.
- Model name.
- GPU/CPU and operating system.
- Cookbook task logs or server logs.

Issues with only "help", "does not work", or a screenshot without context may be closed as not actionable.

## Security

Do not post secrets, API keys, private logs, personal documents, or public IPs in issues or pull requests.

For security reports, follow [SECURITY.md](SECURITY.md).
