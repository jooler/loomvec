.PHONY: infra age-src api worker web admin ops sdk smoke evals lint test backend-test frontend-test

# 基础设施栈
infra:
	cd deploy/compose && docker compose up -d

# 克隆 Apache AGE PG18 源码（构建 postgres-age 镜像的前置步骤，路径被 .gitignore）
age-src:
	git clone --depth 1 --branch PG18 https://github.com/apache/age.git deploy/compose/postgres-age/age-src

api:
	uv run uvicorn loomvec.api.main:app --reload --port 8080

migrate:
	cd services/api && uv run --project ../.. alembic upgrade head

worker:
	uv run celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low

web:
	pnpm dev:web

admin:
	pnpm dev:admin

ops:
	pnpm dev:ops

# 契约导出 + SDK 生成（改 API 后执行）
sdk:
	uv run python scripts/export_openapi.py
	pnpm sdk:generate

# P0-INF-06 冒烟（需 compose 栈与 API 运行中）
smoke:
	uv run python scripts/smoke/run.py

# P1-QA-01 检索评测回归（需 compose 栈 + API + worker 运行中）
evals:
	uv run python evals/run_evals.py

lint:
	uv run ruff check services scripts evals && uv run ruff format --check services scripts evals
	pnpm -r lint

test: backend-test frontend-test

backend-test:
	uv run pytest -m "not integration" -q

frontend-test:
	pnpm -r test
