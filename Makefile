.PHONY: dev dev-backend dev-frontend build-frontend build-release test install

install:
	uv sync --frozen
	cd frontend && npm ci

# Local dev loop: backend with fake identity headers + Vite dev proxy.
dev-backend:
	LOCAL_DEV=1 DEV_FAKE_EMAIL=dev@example.com DEV_GROUPS=platform_admins \
	DATA_ROOT=/tmp/workshop-terminal-dev \
	uv run --frozen uvicorn server.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

dev:
	@echo "Run 'make dev-backend' and 'make dev-frontend' in two terminals."
	@echo "Open http://localhost:5173 (Vite proxies /api and /ws to :8000)."

# Build the React app into static/ — COMMIT the result; Control Tower
# deploys the repo as-cloned with no build step.
build-frontend:
	cd frontend && npm run build

test:
	uv run --frozen python -m pytest tests/ -q

build-release:
	uv run --frozen --no-group dev --group release python scripts/build_release.py
