.PHONY: dev dev-backend dev-frontend build-frontend test install

install:
	python3 -m venv .venv 2>/dev/null || true
	.venv/bin/pip install -r requirements.txt pytest httpx
	cd frontend && npm install

# Local dev loop: backend with fake identity headers + Vite dev proxy.
dev-backend:
	LOCAL_DEV=1 DEV_FAKE_EMAIL=dev@example.com DEV_GROUPS=platform_admins \
	DATA_ROOT=/tmp/workshop-terminal-dev \
	.venv/bin/uvicorn server.main:app --reload --port 8000

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
	.venv/bin/python -m pytest tests/ -q
