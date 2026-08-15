# Konek Két — cổng chất lượng chạy cục bộ.
# `make check` chạy ĐÚNG bộ mà CI chạy (trừ tauri-build, xem `make tauri-build`).

SERVER := server
CLIENT := client

# --- PostgreSQL cho nhóm test `db` ----------------------------------------
#
# Bản cài đích là PostgreSQL 16 (quyết định D4, phase 2). Nhiều máy lập trình
# còn cụm 14/15 của dự án khác đang chiếm 5432, nên mặc định ở đây là cụm 16
# chạy song song trên **5433**:
#
#   brew install postgresql@16
#   # đặt port = 5433 trong /opt/homebrew/var/postgresql@16/postgresql.conf
#   brew services start postgresql@16
#
# Máy nào chạy 16 ngay trên 5432 thì đè lại: `make server-test-db PGPORT=5432`.
# CI không dùng Makefile — nó truyền thẳng ba biến này trong workflow.
PGPORT ?= 5433
PGHOST ?= localhost
DB_TEST_ENV := \
	KET_TEST_DESTRUCTIVE_CLUSTER=1 \
	KET_TEST_ADMIN_DSN=postgresql+psycopg://$(PGHOST):$(PGPORT)/postgres \
	KET_TEST_KET_OWNER_DSN=postgresql+psycopg://ket_owner@$(PGHOST):$(PGPORT)/ket_test \
	KET_TEST_KET_APP_DSN=postgresql+psycopg://ket_app@$(PGHOST):$(PGPORT)/ket_test

.DEFAULT_GOAL := help
.PHONY: help install check \
        server-install server-lint server-format server-typecheck server-imports \
        server-test server-test-db server-coverage server-check version-check \
        client-install client-typecheck client-lint client-build client-check \
        shell-fmt shell-lint tauri-build clean

help: ## Liệt kê lệnh
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: server-install client-install ## Cài phụ thuộc cả hai tầng

check: server-check client-check ## Chạy toàn bộ cổng chất lượng (không gồm tauri build)

# --- server (Python) ------------------------------------------------------

server-install: ## uv sync với Python 3.12
	cd $(SERVER) && uv sync --python 3.12

server-lint: ## ruff
	cd $(SERVER) && uv run ruff check .

server-typecheck: ## mypy --strict trên toàn src/ket
	cd $(SERVER) && uv run mypy

server-imports: ## import-linter — luật phụ thuộc (ADR-004)
	cd $(SERVER) && uv run lint-imports --config importlinter.ini

server-format: ## ruff format --check
	cd $(SERVER) && uv run ruff format --check .

server-test: ## pytest, nhóm không cần DB (gồm kiểm cấm float trong code nghiệp vụ)
	cd $(SERVER) && uv run pytest -m "not db"

server-test-db: ## pytest nhóm cần PostgreSQL 16 thật (RLS, nhật ký bất biến, cô lập dataset)
	cd $(SERVER) && $(DB_TEST_ENV) uv run pytest -m db

version-check: ## Năm tệp khai phiên bản phải khớp (cổng của release.yml)
	python3 .github/scripts/check_version_consistency.py

server-coverage: ## pytest TOÀN BỘ + báo cáo độ phủ (con số CI dán vào PR)
	cd $(SERVER) && $(DB_TEST_ENV) uv run pytest --cov
	cd $(SERVER) && uv run coverage report --format=total | \
		xargs -I{} echo "Độ phủ: {}%"

server-check: server-lint server-format server-typecheck server-imports server-test server-test-db ## Toàn bộ cổng phía server

# --- client (web UI) ------------------------------------------------------

client-install: ## pnpm install
	cd $(CLIENT) && pnpm install --frozen-lockfile

client-typecheck: ## tsc --noEmit
	cd $(CLIENT) && pnpm exec tsc --noEmit

client-lint: ## eslint
	cd $(CLIENT) && pnpm exec eslint .

client-build: ## vite build
	cd $(CLIENT) && pnpm exec vite build

client-check: client-typecheck client-lint client-build ## Toàn bộ cổng phía client

# --- shell (Tauri / Rust) -------------------------------------------------

shell-fmt: ## cargo fmt --check
	cd $(CLIENT)/src-tauri && cargo fmt --check

shell-lint: ## cargo clippy, warning = lỗi
	cd $(CLIENT)/src-tauri && cargo clippy --all-targets -- -D warnings

tauri-build: ## Đóng gói desktop (chậm — tải và biên dịch crate)
	cd $(CLIENT) && pnpm exec tauri build

# --- tiện ích -------------------------------------------------------------

clean: ## Xóa sản phẩm build (giữ nguyên phụ thuộc đã cài)
	rm -rf $(CLIENT)/dist $(CLIENT)/src-tauri/target
	rm -rf $(SERVER)/.mypy_cache $(SERVER)/.ruff_cache $(SERVER)/.pytest_cache
