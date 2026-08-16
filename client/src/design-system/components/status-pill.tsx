/**
 * Nhãn trạng thái — **chấm vuông + chữ**, ba tông. Nguồn: `.w2-s` / `.ok` /
 * `.todo` / `.bad` trong design "Konek Screens 2a".
 *
 * **Trục ngữ nghĩa là "CÓ VIỆC CHO BẠN KHÔNG", không phải "chứng từ đang ở
 * trạng thái nào".** Đây là điểm dễ làm sai nhất của component này, và làm sai
 * thì sai cả cách người dùng đọc màn hình:
 *
 * | Tông | Màu | Nghĩa | Ví dụ trong design |
 * | --- | --- | --- | --- |
 * | `ok` | **xám** | xong rồi, không phải làm gì | "Đã cấp mã", "Đã ghi sổ", "Khớp", "Đã nộp 18/07" |
 * | `todo` | xanh | cần bạn xử lý | "Chờ phát hành", "Chưa lập", "Dưới tồn tối thiểu" |
 * | `bad` | đỏ gạch | hỏng, phải sửa | "Bị từ chối · sai MST", "Lệch 31.600.000", "Thiếu hóa đơn" |
 *
 * Việc **đã xong là XÁM**, không phải xanh lá. Trên một màn hình 200 dòng mà
 * 190 dòng đã xong, tô xanh lá cho chúng làm 10 dòng còn việc chìm nghỉm giữa
 * một biển màu. Xám đẩy việc đã xong lùi khỏi tầm mắt và để màu dành cho thứ
 * cần đọc. Đây là lý do design chỉ có ba tông và không có tông "thành công".
 *
 * Không nền, không viền, không bo tròn — chấm 8×8 đặc + chữ. (Bản trước của
 * component này là chip bo tròn có nền, bốn tông, "success" xanh lá: một bảng
 * màu tự nghĩ ra, không có trong design.)
 *
 * Màu không phải thông tin duy nhất: chữ trong nhãn luôn nói thẳng trạng thái,
 * nên người không phân biệt được đỏ/xanh vẫn đọc được "Bị từ chối".
 */

import type { ReactElement } from 'react'

export type StatusTone = 'ok' | 'todo' | 'bad'

const TONES: Record<StatusTone, string> = {
  ok: 'text-status-ok',
  todo: 'text-status-todo',
  bad: 'text-status-bad',
}

export interface StatusPillProps {
  readonly tone?: StatusTone
  readonly children: string
}

export function StatusPill({ tone = 'ok', children }: StatusPillProps): ReactElement {
  return (
    <span
      data-tone={tone}
      className={`inline-flex items-center gap-1.5 whitespace-nowrap text-meta font-semibold ${TONES[tone]}`}
    >
      {/* Chấm tô bằng `currentColor` nên nó không thể lệch màu khỏi chữ bên
          cạnh — một cặp chấm/chữ khác màu là lỗi chỉ người dùng mới thấy. */}
      <span aria-hidden className="block h-2 w-2 shrink-0 bg-current" />
      {children}
    </span>
  )
}
