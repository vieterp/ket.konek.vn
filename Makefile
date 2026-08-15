# Konek Két — cổng chất lượng chạy cục bộ.
# `make check` chạy ĐÚNG bộ mà CI chạy (trừ tauri-build, xem `make tauri-build`).

SERVER := server
CLIENT := client

.DEFAULT_GOAL := help
.PHONY: help install check \
        server-install server-lint server-format server-typecheck server-imports \
        server-test server-test-db server-check \
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

server-test-db: ## pytest nhóm cần PostgreSQL thật (RLS, nhật ký bất biến, cô lập dataset)
	cd $(SERVER) && uv run pytest -m db

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
