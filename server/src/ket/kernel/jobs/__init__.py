"""Hàng đợi job nặng trong PostgreSQL (ADR-014).

Import gói này **đăng ký luôn** các loại job nền tảng (`builtin`). Có chủ đích:
API và worker là hai tiến trình khác nhau, và một registry rỗng ở tiến trình nào
trong hai bên đều hỏng âm thầm — API sẽ từ chối một loại job hợp lệ, còn worker
sẽ đánh hỏng mọi job nó nhận. Buộc mỗi điểm vào tự nhớ `import ket.kernel.jobs.builtin`
là đúng thứ sẽ bị quên ở tiến trình thứ ba (`ket.admin`, hoặc bản đóng gói S4).
"""

from ket.kernel.jobs import builtin as builtin
