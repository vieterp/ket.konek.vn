---
adr: 015
title: "Kỷ luật kiểu tĩnh (mypy strict + Pydantic v2)"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-002, ADR-014]
---

# ADR-015: Kỷ luật kiểu tĩnh (mypy strict + Pydantic v2)

## Context

Python không có kiểm kiểu lúc compile. Trên miền 504 FR, thêm kiểm kiểu sau 6 tháng là **bất khả thi** — developer quen "chạy rồi mới test". Hàng rào duy nhất bù cho việc này: **mypy strict từ commit đầu tiên**.

**LD-13**: Mypy/pyright **strict** trong CI từ phase 1; Pydantic v2 ở mọi ranh giới API; cấm `dict[str, Any]` qua ranh giới module; cấm `float` trong code nghiệp vụ.

## Decision

1. **mypy strict** (hoặc pyright strict) chạy CI, fail = block merge.
   - Cấu hình: `disallow_untyped_defs`, `disallow_any_generics`, `warn_return_any`, `strict_equality`, `no_implicit_optional`.
   - **Không** ngoại lệ per-module ở giai đoạn này (repo rỗng — dễ nhất).
   - Toàn bộ `src/konek` bắt buộc xanh; `tests/` có thể relax một chút.
   - Thêm `# type: ignore` phải kèm **lý do** dòng cụ thể.

2. **Pydantic v2** cho mọi ranh giới API:
   - Request model: `class VoucherCreate(BaseModel)` với type hint đầy đủ.
   - Response model: `class VoucherOut(BaseModel)` explicit.
   - **Không** `dict[str, Any]` qua ranh giới module (trong serialization nếu cần → dùng `model_dump()`, kiểm lại kiểu).

3. **Cấm `float` trong code nghiệp vụ**:
   - Chỉ `decimal.Decimal` cho tiền tệ.
   - Cấm literal `1.5` trong `konek.posting/modules/*`.
   - Test `server/tests/test_no_float_in_domain.py` quét AST (bắt tham chiếu tên `float`, thuộc tính `.float`, literal dấu phẩy động).

4. **Dataclass hoặc Pydantic cho DTO nội bộ**:
   - DTO giữa module: dataclass + type hint (nhanh, đủ dùng nội bộ).
   - Không cần Pydantic validate ở nội bộ, chỉ ở API boundary.

## Consequences

### Tích cực

- **Bắt lỗi sớm**: type error = block PR, không chạy tới test.
- **Developer hiệu suất**: IDE autocomplete chính xác, refactor an toàn.
- **Tài liệu sống**: type hint = hợp đồng code, không cần docstring dài.
- **Làm việc với dữ liệu tài chính**: `Decimal` buộc developer suy nghĩ về rounding.

### Tiêu cực / Đánh đổi

- **Gõ nhiều hơn**: kiểu hint chi tiết → dòng code dài, đọc khó.
- **Refactor chậm đầu**: thêm kiểu khi viết, không phải sau.
- **Lỗi Pydantic thực thi**: khi instance fail validate, traceback là JSON error (cần debug kỹ).
- **Dependency thư 3**: nếu lib không type hint, phải type stubs riêng.

## Reversal cost

- **Xóa mypy**: mất hàng rào duy nhất → sai số dễ vào production.
- **Bỏ Pydantic**: dùng `dict` lại → loss of validate, mất documentation.
- Phải sửa tất cả module `konek.*` loại bỏ type hint (không khả thi sau 1 tháng).

## Related FR

- **LD-13**: Ràng buộc thiết kế.
- **FR-NFR-001..007**: Đúng số liệu — Decimal + strict type ngăn lỗi tính toán.
- Liên quan **ADR-014** (SQL code cũng phải type-safe).

---

**Phase 1 setup**: 
- Cấu hình `pyproject.toml` [tool.mypy]
- Test: `mypy --strict src/konek` pass 0 error
- Test: `pytest server/tests/test_no_float_in_domain.py` → AST quét detect literal `float` → fail
- Note: mypy cấu hình `packages = ["konek"]` nên `tests/` không nằm trong phạm vi mypy (relax một chút, chỉ check `src/`)
