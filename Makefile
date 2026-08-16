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
	KET_TEST_KET_APP_DSN=postgresql+psycopg://ket_app@$(PGHOST):$(PGPORT)/ket_test \
	KET_TEST_KET_WORKER_DSN=postgresql+psycopg://ket_worker@$(PGHOST):$(PGPORT)/ket_test

.DEFAULT_GOAL := help
.PHONY: help install check \
        server-install server-lint server-format server-typecheck server-imports \
        server-test server-test-db server-coverage server-check version-check \
        client-install client-typecheck client-lint client-test client-build client-check \
        api-types api-types-check \
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

client-test: ## vitest — máy trạng thái đăng nhập, hợp đồng HTTP, i18n
	cd $(CLIENT) && pnpm exec vitest run

client-build: ## vite build
	cd $(CLIENT) && pnpm exec vite build

client-check: api-types-check client-typecheck client-lint client-test client-build ## Toàn bộ cổng phía client

# --- hợp đồng client↔server (OpenAPI → TypeScript) ------------------------
#
# Nguồn sự thật là mã server (LD-03). `openapi.json` và `schema.d.ts` đều được
# COMMIT, và CI sinh lại rồi so `git diff`: đổi response model mà quên sinh lại
# thì cổng đỏ, và người review thấy hợp đồng đổi gì ngay trong diff.

API_TYPES_DIR := $(CLIENT)/packages/api-types

api-types: ## Sinh lại type TypeScript từ OpenAPI của server
	cd $(SERVER) && uv run python scripts/export_openapi.py ../$(API_TYPES_DIR)/openapi.json
	cd $(CLIENT) && pnpm exec openapi-typescript packages/api-types/openapi.json \
		-o packages/api-types/schema.d.ts

api-types-check: api-types ## Đỏ nếu type sinh lại khác bản đã commit
	@# Hai phép kiểm, vì `git diff` có một điểm mù: nó **không thấy tệp chưa được
	@# theo dõi**. Một `schema.d.ts` chưa từng commit sẽ làm cổng xanh rỗng — đúng
	@# lúc nó phải đỏ nhất.
	@test -z "$$(git ls-files --others --exclude-standard -- $(API_TYPES_DIR))" || { \
		echo "Type sinh cho client chưa được đưa vào git:"; \
		git ls-files --others --exclude-standard -- $(API_TYPES_DIR); \
		echo "Chạy \`make api-types\` rồi commit thư mục $(API_TYPES_DIR)."; \
		exit 1; }
	@git diff --exit-code -- $(API_TYPES_DIR) || { \
		echo ""; \
		echo "Hợp đồng API đã đổi nhưng type sinh cho client chưa được commit."; \
		echo "Chạy \`make api-types\` rồi commit thư mục $(API_TYPES_DIR)."; \
		exit 1; }

# --- shell (Tauri / Rust) -------------------------------------------------

shell-fmt: ## cargo fmt --check
	cd $(CLIENT)/src-tauri && cargo fmt --check

shell-lint: ## cargo clippy, warning = lỗi
	cd $(CLIENT)/src-tauri && cargo clippy --all-targets -- -D warnings

# `createUpdaterArtifacts` trong tauri.conf.json khiến mọi lần build đều sinh gói
# cập nhật và ĐỔ nếu thiếu khóa ký. Khóa nằm ngoài repo (~/.konek/), giống hệt
# cách CI lấy nó từ GitHub Secrets.
KET_UPDATER_KEY ?= $(HOME)/.konek/ket-updater.key

tauri-build: ## Đóng gói desktop (chậm — tải và biên dịch crate; cần khóa ký updater)
	@test -f "$(KET_UPDATER_KEY)" || { \
		echo "Thiếu khóa ký updater tại $(KET_UPDATER_KEY)."; \
		echo "Sinh mới: cd client && pnpm exec tauri signer generate -w $(KET_UPDATER_KEY)"; \
		echo "(khóa công khai trong tauri.conf.json phải khớp, nếu không bản cũ sẽ từ chối cập nhật)"; \
		exit 1; }
	cd $(CLIENT) && \
		TAURI_SIGNING_PRIVATE_KEY="$$(cat $(KET_UPDATER_KEY))" \
		TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$$(cat $(KET_UPDATER_KEY).password 2>/dev/null || cat $(HOME)/.konek/ket-updater.password)" \
		pnpm exec tauri build

# --- tiện ích -------------------------------------------------------------

clean: ## Xóa sản phẩm build (giữ nguyên phụ thuộc đã cài)
	rm -rf $(CLIENT)/dist $(CLIENT)/src-tauri/target
	rm -rf $(SERVER)/.mypy_cache $(SERVER)/.ruff_cache $(SERVER)/.pytest_cache
