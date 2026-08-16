/**
 * Định dạng số, tiền và ngày để **hiển thị**.
 *
 * Ràng buộc kiến trúc quan trọng nhất của tệp này (LD-03, docs/design-guidelines
 * §5): **không có phép tính tiền ở client**. Không cộng, không trừ, không nhân
 * tỷ giá, không làm tròn lại. Server tính bằng `Decimal` với `ROUND_HALF_UP` và
 * trả về con số cuối cùng; ở đây chỉ đổi cách viết nó ra màn hình.
 *
 * Hệ quả: mọi hàm dưới đây nhận **chuỗi** cho số tiền, đúng như JSON của server
 * trả về. Chuyển sang `number` là chỗ mất chính xác đầu tiên — `0.1 + 0.2` của
 * IEEE-754 không phải câu chuyện lý thuyết khi cột tiền có 15 chữ số.
 */

import type { Locale } from '@/lib/i18n'

const LOCALE_TAGS: Record<Locale, string> = { vi: 'vi-VN', en: 'en-US' }

/**
 * Số tiền cho cột bảng: nhóm hàng nghìn, giữ nguyên số chữ số thập phân server gửi.
 *
 * `Intl.NumberFormat` nhận trực tiếp chuỗi số từ ES2023, nên không phải đi qua
 * `Number` — đúng bất biến "không chuyển đổi số tiền" ở trên.
 */
export function formatMoney(amount: string, locale: Locale): string {
  const decimals = amount.split('.')[1]?.length ?? 0
  const formatter = new Intl.NumberFormat(LOCALE_TAGS[locale], {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
  // `format` nhận thẳng chuỗi số từ ES2023 (`StringNumericLiteral`), nhưng khai
  // báo kiểu của lib còn là `number`. Ép kiểu ở đúng một dòng này thay vì để
  // `Number(amount)` len vào — đó mới là chỗ mất chính xác thật.
  const numericFormatter = formatter as unknown as { format(value: string): string }
  return numericFormatter.format(amount)
}

/** Ngày chứng từ: `dd/MM/yyyy` ở vi, định dạng địa phương ở en. */
export function formatDate(iso: string, locale: Locale): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) {
    return iso
  }
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], { dateStyle: 'short' }).format(parsed)
}

/** Thời điểm có giờ — dùng cho nhật ký và hạn phiên đăng nhập. */
export function formatDateTime(iso: string, locale: Locale): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) {
    return iso
  }
  return new Intl.DateTimeFormat(LOCALE_TAGS[locale], {
    dateStyle: 'short',
    timeStyle: 'short',
  }).format(parsed)
}
