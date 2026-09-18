/**
 * Cột gộp "Trạng thái với CQT", cột "Khách nhận" và việc tiếp theo của một
 * dòng lưới hóa đơn điện tử (U3, design nhóm 02 — "trạng thái với cơ quan
 * thuế là thông tin số 1").
 *
 * Trạng thái máy 7D thành MỘT câu người dùng đọc, kèm lý do từ chối
 * (`tax_authority_message`) hiện thẳng trên ô — `reject` là cạnh duy nhất ghi
 * cột CQT "từ chối" và nó luôn đi cùng `PHAT_HANH_LOI`, nên trục CQT không cần
 * đọc riêng (cùng lý do server định nghĩa tab `can-xu-ly` = trạng thái 2). Trục
 * ngữ nghĩa vẫn là "có việc cho bạn không" (design-guidelines §5 ba tông):
 * chờ phát hành = `todo`; bị từ chối / lỗi = `bad`; đã cấp mã, đã gửi, và ba
 * trạng thái cuối đã xong việc = `ok`.
 *
 * Hàm thuần, không React — bài test ghim từng nhánh.
 */

import type { StatusTone } from '@/design-system/components'
import type { Locale, Translate } from '@/lib/i18n'
import { formatDate } from '@/lib/formatters'

import type { EInvoiceListItem } from './use-einvoices'

/** Khớp `EInvoiceStatus` phía server (7D). */
export const EINVOICE_STATUS_DRAFT = 0
export const EINVOICE_STATUS_ISSUING = 1
export const EINVOICE_STATUS_ISSUE_FAILED = 2
export const EINVOICE_STATUS_ISSUED = 3
export const EINVOICE_STATUS_SENT = 4
export const EINVOICE_STATUS_REPLACED = 5
export const EINVOICE_STATUS_ADJUSTED = 6
export const EINVOICE_STATUS_CANCELLED = 7

/** Từ trạng thái này trở lên tờ đã ra khỏi phần mềm — có bản thể hiện để xem. */
export const EINVOICE_ISSUED_FLOOR = EINVOICE_STATUS_ISSUED

/**
 * Tờ chặn lập tờ thứ hai cho cùng chứng từ — tập của chỉ mục
 * `uq_einvoices_live_source_voucher` (đang phát hành / đã cấp mã / đã gửi).
 * KHÔNG phải `einvoice.models.LIVE_STATUSES` (tập ấy thêm "đã điều chỉnh" cho
 * FR-EIV-035 — tờ điều chỉnh thông tin nằm cùng chứng từ với tờ bị điều chỉnh).
 */
export const LIVE_STATUSES: readonly number[] = [
  EINVOICE_STATUS_ISSUING,
  EINVOICE_STATUS_ISSUED,
  EINVOICE_STATUS_SENT,
]

export type EInvoiceNextAction = 'issue' | 'reissue' | 'send' | null

export interface EInvoiceRowStatus {
  readonly tone: StatusTone
  readonly label: string
  readonly nextAction: EInvoiceNextAction
  readonly actionLabel: string | null
  /** Cột "Khách nhận": chữ + tông; `null` = dấu gạch (tờ chưa tới lúc gửi). */
  readonly delivery: { readonly label: string; readonly tone: StatusTone } | null
}

/** Số hóa đơn hiện trên lưới: "ký hiệu · số", hoặc "chưa cấp số" khi trống. */
export function invoiceNumberText(t: Translate, row: EInvoiceListItem): string {
  return row.invoice_no === null
    ? t('einvoice.list.noNumberYet')
    : `${row.serial} ${row.invoice_no}`
}

export function einvoiceRowStatus(
  t: Translate,
  locale: Locale,
  row: EInvoiceListItem,
): EInvoiceRowStatus {
  const none = { nextAction: null, actionLabel: null, delivery: null } as const
  switch (row.status) {
    case EINVOICE_STATUS_DRAFT:
      return {
        tone: 'todo',
        label: t('einvoice.list.status.draft'),
        nextAction: 'issue',
        actionLabel: t('einvoice.list.action.issue'),
        delivery: null,
      }
    case EINVOICE_STATUS_ISSUING:
      return {
        tone: 'todo',
        label: t('einvoice.list.status.issuing'),
        nextAction: null,
        actionLabel: t('einvoice.list.action.waitTaxAuthority'),
        delivery: null,
      }
    case EINVOICE_STATUS_ISSUE_FAILED:
      return {
        tone: 'bad',
        label:
          row.tax_authority_message === null || row.tax_authority_message === ''
            ? t('einvoice.list.status.rejected')
            : t('einvoice.list.status.rejectedWith', { reason: row.tax_authority_message }),
        nextAction: 'reissue',
        actionLabel: t('einvoice.list.action.reissue'),
        delivery: null,
      }
    case EINVOICE_STATUS_ISSUED:
      return {
        tone: 'ok',
        label: t('einvoice.list.status.issued'),
        nextAction: 'send',
        actionLabel: t('einvoice.list.action.send'),
        delivery: { label: t('einvoice.list.delivery.notSent'), tone: 'todo' },
      }
    case EINVOICE_STATUS_SENT:
      return {
        tone: 'ok',
        label: t('einvoice.list.status.issued'),
        ...none,
        delivery: {
          label: t('einvoice.list.delivery.sent', {
            date: row.sent_at === null ? '' : formatDate(row.sent_at, locale),
          }),
          tone: 'ok',
        },
      }
    case EINVOICE_STATUS_REPLACED:
      return {
        tone: 'ok',
        label:
          row.superseded_by_no == null
            ? t('einvoice.list.status.replaced')
            : t('einvoice.list.status.replacedBy', { no: row.superseded_by_no }),
        ...none,
      }
    case EINVOICE_STATUS_ADJUSTED:
      return {
        tone: 'ok',
        label:
          row.superseded_by_no == null
            ? t('einvoice.list.status.adjusted')
            : t('einvoice.list.status.adjustedBy', { no: row.superseded_by_no }),
        ...none,
      }
    case EINVOICE_STATUS_CANCELLED:
      return { tone: 'ok', label: t('einvoice.list.status.cancelled'), ...none }
    default:
      return { tone: 'ok', label: String(row.status), ...none }
  }
}

/** Tờ đã ra khỏi phần mềm — có bản thể hiện, và là đối tượng của luồng sai sót. */
export function isIssued(row: { readonly status: number }): boolean {
  return row.status >= EINVOICE_ISSUED_FLOOR
}

/** Tờ còn HIỆU LỰC để xử lý sai sót (đã cấp mã hoặc đã gửi) — ba trạng thái cuối không có cạnh ra. */
export function canResolveError(row: { readonly status: number }): boolean {
  return row.status === EINVOICE_STATUS_ISSUED || row.status === EINVOICE_STATUS_SENT
}
