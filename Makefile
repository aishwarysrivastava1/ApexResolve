# ApexResolve — every day-to-day command in one place. Run `make help` for the list.
# Docker commands read secrets from .env (copy .env.example first). Local commands use backend/.venv.

COMPOSE := docker compose --env-file .env -f deploy/docker-compose.yml
VENV    := backend/.venv
PY      := $(VENV)/bin/python

.PHONY: help env dev-setup up demo down logs ps demo-reset backup rotate-ledger-key verify-ledger \
        check-integrity demo-tamper demo-truncate test coverage test-unit e2e lint security golden golden-check simulate web-dev web-check

help: ## list the targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

env: ## create .env from the example (then fill in every empty value by hand)
	@test -f .env && echo ".env already exists" || (cp .env.example .env && echo "created .env: now fill in the passwords")

# ---------- running the system (Docker) ----------
up: ## build and start everything (no demo data)
	$(COMPOSE) up -d --build

demo: ## build and start everything, with the synthetic demo data
	$(COMPOSE) --profile demo up -d --build

down: ## stop everything (data is kept)
	$(COMPOSE) --profile demo down

logs: ## follow the api and worker logs
	$(COMPOSE) logs -f api worker

ps: ## show container health
	$(COMPOSE) ps

demo-reset: ## DEMO ONLY: wipe the database and reseed (keeps the ledger key and HTTPS certificates)
	$(COMPOSE) --profile demo down
	docker volume rm apexresolve_pgdata
	$(COMPOSE) --profile demo up -d

backup: ## dump the database to backups/ (copy the file off the host afterwards)
	mkdir -p backups
	$(COMPOSE) exec -T db pg_dump -U postgres -d apex -Fc > backups/apex-$$(date +%Y%m%d-%H%M%S).dump
	@ls -lh backups | tail -1

rotate-ledger-key: ## make a new ledger key; set a NEW LEDGER_KEY_ID in .env first
	$(COMPOSE) run --rm --no-deps keygen sh -c "rm -f /app/keys/ledger_signing_key.pem && python scripts/make_ledger_key.py /app/keys/ledger_signing_key.pem"
	$(COMPOSE) up -d --force-recreate api worker

verify-ledger: ## verify the whole hash chain and every signature
	$(COMPOSE) exec api python scripts/verify_ledger.py

check-integrity: ## run the reconciliation queries (double entry, paid once, timers, ledger head)
	$(COMPOSE) exec api python scripts/check_integrity.py

demo-tamper: ## DEMO ONLY: flip the verdict of the latest decision (verify must then fail)
	$(COMPOSE) run --rm --no-deps migrate python scripts/demo_ledger_attack.py tamper

demo-truncate: ## DEMO ONLY: delete the last 3 ledger events (verify with a checkpoint must then fail)
	$(COMPOSE) run --rm --no-deps migrate python scripts/demo_ledger_attack.py truncate 3

# ---------- developing (local Python 3.12 and Node 22) ----------
dev-setup: ## create backend/.venv with the locked dev dependencies, and install web packages
	python3.12 -m venv $(VENV)
	$(VENV)/bin/pip install -r backend/requirements-dev.lock
	$(PY) -m playwright install chromium
	cd web && npm ci

test: ## all backend tests (unit, property, integration on an embedded PostgreSQL)
	cd backend && ../$(PY) -m pytest -q

coverage: ## tests with the CI coverage gates (backend >= 80%, app/domain >= 95%)
	cd backend && ../$(PY) -m pytest -q --cov=app --cov-fail-under=80
	cd backend && ../$(PY) -m coverage report --include="app/domain/*" --fail-under=95

test-unit: ## fast tests only (no database)
	cd backend && ../$(PY) -m pytest -q tests/unit

e2e: ## browser tests T-E2E-01…06 (starts the IdP, api, worker and Vite itself; screenshots go to /tmp/apexresolve-e2e)
	cd backend && E2E=1 ../$(PY) -m pytest -q tests/e2e

lint: ## ruff (style and bug patterns)
	cd backend && ../$(VENV)/bin/ruff check .

security: ## bandit (code) and pip-audit (dependencies)
	cd backend && ../$(VENV)/bin/bandit -q -ll -r app devidp scripts
	cd backend && ../$(VENV)/bin/pip-audit -r requirements.lock

golden: ## regenerate the golden decision vectors (only after a reviewed policy change)
	cd backend && ../$(PY) scripts/gen_golden.py

golden-check: ## fail if the committed golden vectors differ from what the engine produces
	cd backend && ../$(PY) scripts/gen_golden.py && git diff --exit-code tests/golden

simulate: ## print the synthetic threshold-calibration table
	cd backend && ../$(PY) scripts/simulate.py

web-dev: ## run the SPA with hot reload (proxies /api and /idp to the local containers)
	cd web && npm run dev

web-check: ## type-check, lint and build the SPA
	cd web && npm run typecheck && npm run lint && npm run build
