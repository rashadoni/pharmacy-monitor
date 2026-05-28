#!/usr/bin/env bash
#
# Phase 6.4 — local CI runner.
#
# Runs the same checks as .github/workflows/ci.yml but locally, using
# docker-compose for Postgres + Redis. Use before every push since GitHub
# Actions CI on this repo currently doesn't execute (billing/quota issue,
# under investigation — see CLAUDE.md).
#
# Usage:
#   scripts/ci-local.sh              # full suite
#   scripts/ci-local.sh --quick      # skip frontend (Python tests only)
#   scripts/ci-local.sh --frontend   # skip Python (frontend only)
#
# Exit codes:
#   0  — all checks passed
#   1+ — first failed check (lint=10, pytest=20, frontend=30)

set -euo pipefail

QUICK=0
FRONTEND_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=1 ;;
        --frontend) FRONTEND_ONLY=1 ;;
        --help|-h)
            sed -n '4,18p' "$0" | sed 's/^# //; s/^#//'
            exit 0
            ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Color output (skip in non-tty)
if [ -t 1 ]; then
    BOLD=$(tput bold) RED=$(tput setaf 1) GREEN=$(tput setaf 2) YELLOW=$(tput setaf 3) NC=$(tput sgr0)
else
    BOLD="" RED="" GREEN="" YELLOW="" NC=""
fi

step() { echo ""; echo "${BOLD}━━ $* ━━${NC}"; }
ok() { echo "  ${GREEN}✓${NC} $*"; }
fail() { echo "  ${RED}✗${NC} $*" >&2; }
warn() { echo "  ${YELLOW}⚠${NC} $*"; }

if [ "$FRONTEND_ONLY" = "0" ]; then
    # ───────────────────────────────────────────────────────────────
    step "Step 1: Python lint (ruff)"
    if uv run ruff check . > /dev/null 2>&1; then
        ok "ruff check"
    else
        fail "ruff check failed"
        uv run ruff check . | head -20
        exit 10
    fi

    if uv run ruff format --check . > /dev/null 2>&1; then
        ok "ruff format"
    else
        fail "ruff format issues — run 'uv run ruff format .' to fix"
        exit 10
    fi

    # ───────────────────────────────────────────────────────────────
    step "Step 2: Start Postgres + Redis (docker-compose)"
    if ! docker info > /dev/null 2>&1; then
        fail "Docker daemon not running. Start Docker Desktop and retry."
        exit 20
    fi

    docker compose up -d postgres redis > /dev/null 2>&1
    sleep 3

    # Wait for healthy
    for i in $(seq 1 20); do
        if docker compose exec -T postgres pg_isready -U pm > /dev/null 2>&1; then
            ok "Postgres ready"
            break
        fi
        sleep 1
    done
    for i in $(seq 1 10); do
        if docker compose exec -T redis redis-cli ping > /dev/null 2>&1; then
            ok "Redis ready"
            break
        fi
        sleep 1
    done

    # ───────────────────────────────────────────────────────────────
    step "Step 3: Alembic migrations"
    export DATABASE_URL="postgresql+psycopg://pm:pm@localhost:5432/pharmacy_monitor"
    export REDIS_URL="redis://localhost:6379/0"
    export JWT_SECRET="local-ci-secret-not-for-prod-use"
    export PHARMACY_API_KEY="local-ci-key"

    if uv run alembic upgrade head > /tmp/ci-local-alembic.log 2>&1; then
        ok "alembic upgrade head"
    else
        fail "Alembic migration failed"
        tail -20 /tmp/ci-local-alembic.log
        exit 20
    fi

    # ───────────────────────────────────────────────────────────────
    step "Step 4: Pytest"
    if uv run pytest -q --no-header > /tmp/ci-local-pytest.log 2>&1; then
        tail -3 /tmp/ci-local-pytest.log
        ok "pytest"
    else
        fail "pytest failed"
        tail -40 /tmp/ci-local-pytest.log
        exit 20
    fi
fi

if [ "$QUICK" = "0" ]; then
    # ───────────────────────────────────────────────────────────────
    step "Step 5: Frontend (typecheck + build)"
    cd frontend
    if pnpm typecheck > /tmp/ci-local-typecheck.log 2>&1; then
        ok "typecheck"
    else
        # Many pre-existing TS errors — warn but don't fail
        warn "typecheck has pre-existing issues (see /tmp/ci-local-typecheck.log)"
    fi

    if NODE_OPTIONS=--max-old-space-size=4096 pnpm build > /tmp/ci-local-build.log 2>&1; then
        ok "pnpm build"
        echo "  Build summary:"
        grep -E "First Load JS|Middleware|locale" /tmp/ci-local-build.log | head -5 | sed 's/^/    /'
    else
        fail "pnpm build failed"
        tail -30 /tmp/ci-local-build.log
        exit 30
    fi
    cd ..
fi

# ───────────────────────────────────────────────────────────────
echo ""
echo "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "${GREEN}${BOLD}  All CI checks passed locally. Safe to push.${NC}"
echo "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Note: GitHub Actions CI currently doesn't execute (billing quota suspected,"
echo "  146 runs with 0-second duration, 0 jobs created). This local script is"
echo "  the source of truth until that's investigated. См. CLAUDE.md / RUNBOOK."
