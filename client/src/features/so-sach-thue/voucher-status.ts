/**
 * Máy trạng thái chứng từ (SRS 00 §3.3, FR-NFR-003) — chỉ ba trạng thái server
 * dùng ở phase 4. "Đã khóa sổ" KHÔNG phải trạng thái riêng: nó là dẫn xuất từ
 * kỳ của chứng từ đã khóa hay chưa, không nằm trên `status`.
 */

import type { StatusTone } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

export const VOUCHER_STATUS_DRAFT = 1
export const VOUCHER_STATUS_POSTED = 2
export const VOUCHER_STATUS_CANCELLED = 4

/** Chưa ghi sổ là VIỆC còn phải làm (xanh), đã ghi sổ là xong rồi (xám) — StatusPill §quy ước. */
export function voucherStatusTone(status: number): StatusTone {
  if (status === VOUCHER_STATUS_POSTED) {
    return 'ok'
  }
  if (status === VOUCHER_STATUS_CANCELLED) {
    return 'bad'
  }
  return 'todo'
}

export function voucherStatusLabel(t: Translate, status: number): string {
  if (status === VOUCHER_STATUS_POSTED) {
    return t('gl.voucher.status.posted')
  }
  if (status === VOUCHER_STATUS_CANCELLED) {
    return t('gl.voucher.status.cancelled')
  }
  return t('gl.voucher.status.draft')
}

/** "Việc tiếp theo" (U1) — chỉ Đã cất còn việc; Đã ghi sổ/Đã hủy là xong. */
export function voucherNextAction(t: Translate, status: number): string | null {
  return status === VOUCHER_STATUS_DRAFT ? t('gl.voucher.nextAction.post') : null
}
