# Justfile — common local dev tasks for Odysseus.
# Requires `just` (https://github.com/casey/just). Run `just` with no args to
# list recipes. CI invokes these same tools directly (.github/workflows/ci.yml);
# this file is convenience for local use. Activate the project's virtualenv
# first so pytest/ruff/mypy/uvicorn resolve from it.

host := "127.0.0.1"
port := "7000"

# List available recipes.
default:
    @just --list

# Run the dev server with autoreload (uvicorn via `python -m`, per launch-windows.ps1).
dev:
    python -m uvicorn app:app --host {{host}} --port {{port}} --reload

# Run the test suite.
test:
    pytest -q

# Lint with ruff.
lint:
    ruff check .

# Format with ruff.
fmt:
    ruff format .

# Type-check with mypy (gradual; config in pyproject.toml).
typecheck:
    mypy app.py src core services routes mcp_servers

# Build the stack, wait for /healthz, then tear down (needs Docker; Phase 0.3 expands this).
smoke:
    docker compose up -d --build
    @echo "Waiting for /healthz on {{host}}:{{port}} ..."
    timeout 90 sh -c 'until curl -fsS http://{{host}}:{{port}}/healthz >/dev/null; do sleep 2; done'
    curl -fsS http://{{host}}:{{port}}/healthz
    docker compose down
