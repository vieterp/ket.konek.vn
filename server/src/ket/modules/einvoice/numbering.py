"""Dãy số hóa đơn — phạm vi (mẫu số, ký hiệu), liên tục không thủng (BR-EIV-02).

Số hóa đơn khác số chứng từ ở đúng hai điểm, và cả hai đều ở đây:

* **Phạm vi** không phải (loại chứng từ, chi nhánh, chu kỳ) mà là (mẫu số, ký
  hiệu) — hai trục đến từ hồ sơ đăng ký với cơ quan thuế. Vì vậy dãy khai bằng
  `NumberingService.define_by_scope_key` và cấp bằng `allocate_by_scope_key`,
  hai đường "khóa phạm vi tường minh" của kernel, chứ không qua `NumberingRule`.
* **Không được thủng** (`allow_gaps=False`), nên mỗi số cấp ra để lại một dòng
  `allocated_numbers` truy được về hóa đơn đã tiêu nó. Đó cũng là lý do lượt
  cấp số phải nằm trong chính transaction ghi hóa đơn: rollback trả số lại, và
  một transaction riêng đã commit sẽ tiêu mất số của một hóa đơn chưa từng tồn
  tại (xem docstring `kernel/numbering/service.py`).

Dãy **không reset theo năm**, khác mặc định `YEARLY` của chứng từ nội bộ: ký
hiệu hóa đơn theo NĐ123/TT78 đã mang năm trong chính nó (`C26TAA` — `26` là
2026), nên sang năm là sang một ký hiệu mới, tức một dãy mới, mà không cần ai
reset gì. Reset theo năm trên cùng một ký hiệu sẽ cấp lại số `00000001` cho năm
sau và đụng ngay `uq_einvoices_form_number`.
"""

from __future__ import annotations

from ket.kernel.numbering.service import SCOPE_SEPARATOR

EINVOICE_SCOPE_ROOT = "EIV"
"""Đoạn đầu của mọi khóa phạm vi hóa đơn — `EIV|{mẫu số}|{ký hiệu}`."""

INVOICE_NUMBER_PADDING = 8
"""Số hóa đơn có **8 chữ số** (NĐ123/2020 điều 10 khoản 3): `00000001`.

Không phải một lựa chọn thẩm mỹ — độ dài này in trên tờ hóa đơn và nộp lên cơ
quan thuế, nên nó là một con số của pháp luật, không của cấu hình.
"""


def scope_key_for(*, form_no: str, serial: str) -> str:
    """Khóa phạm vi dãy số của một cặp (mẫu số, ký hiệu).

    Ký tự ngăn cách mượn thẳng `SCOPE_SEPARATOR` của kernel thay vì gõ lại `"|"`:
    khóa này nằm chung một cột `scope_key` với khóa của mọi dãy chứng từ, và
    hai quy ước ngăn cách trong một cột là một chỗ để tách ngược ra sai.
    """
    if SCOPE_SEPARATOR in form_no or SCOPE_SEPARATOR in serial:
        raise ValueError(
            f"Mẫu số và ký hiệu hóa đơn không được chứa ký tự {SCOPE_SEPARATOR!r} — "
            "nó là ký tự ngăn cách của khóa phạm vi dãy số"
        )
    return SCOPE_SEPARATOR.join((EINVOICE_SCOPE_ROOT, form_no, serial))
