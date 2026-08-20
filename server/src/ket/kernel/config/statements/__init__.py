"""Layout BCTC theo công thức (FR-GLE-043) — schema + grammar, thuộc gói cấu hình.

Nằm trong `kernel.config` chứ không trong `reporting` (khác phác thảo phase-05,
quyết định lát 5B): loader/seeder/importer gói cấu hình — đều là `kernel` —
phải **kiểm công thức fail-closed** ngay lúc đọc dữ liệu gói, mà luật phụ thuộc
C1 cấm `kernel` import `reporting`. Grammar + evaluator là phép toán thuần
(không SQL, không FastAPI) nên đứng ở kernel là đúng tầng; phần đọc số từ sổ
cái (`statement_builder`) mới thuộc `reporting`.
"""
