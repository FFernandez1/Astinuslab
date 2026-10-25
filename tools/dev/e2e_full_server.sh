#!/usr/bin/env bash
# Tier-3 full-E2E web server: init DB, start a Celery worker, then run uvicorn.
#
# The full tier boots real libvirt VMs, whose provisioning runs on the Celery
# worker. The smoke/integration tiers mock those POSTs and need only uvicorn,
# so this wrapper is used exclusively by playwright.config.js when
# ASTINUSLAB_E2E_FULL=1. Playwright terminates this process on teardown; the
# trap ensures the background worker dies with it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
PYTHON_BIN="${ASTINUSLAB_PYTHON:-$REPO_ROOT/.venv/bin/python}"

"$PYTHON_BIN" -m astinuslab.dbcli init

"$PYTHON_BIN" -m celery -A astinuslab.jobs.celery_main worker \
    --concurrency=1 --pool=threads --loglevel=INFO &
WORKER_PID=$!
UVICORN_PID=""

cleanup() {
    kill "$WORKER_PID" "$UVICORN_PID" 2>/dev/null || true
    wait "$WORKER_PID" "$UVICORN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$PYTHON_BIN" -m uvicorn astinuslab.main:app --host 127.0.0.1 --port 8000 &
UVICORN_PID=$!
wait "$UVICORN_PID"
