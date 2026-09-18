/**
 * Trạng thái + việc tiếp theo của một dòng lưới chứng từ bán hàng (U1, bộ xương
 * màn 01 dùng lại).
 *
 * Trục ngữ nghĩa là "có việc cho bạn không" (docs/design-guidelines.md §5 ba
 * tông): chưa ghi sổ = `todo`; quá hạn / thiếu hóa đơn = `bad`; đã ghi sổ còn
 * nợ chưa tới hạn và đã thu đủ đều là `ok` — xám, lùi khỏi tầm mắt.
 *
 * "Thiếu hóa đơn" là định nghĩa của SERVER (`einvoice=missing`, cùng hằng với
 * BFF `pending-issues`): đã ghi sổ, loại cần hóa đơn, không tờ HĐĐT còn sống.
 * Client chỉ đọc lại `has_live_einvoice` + `kind` — bảng loại cần hóa đơn chép
 * từ `sales.models.KINDS_NEEDING_EINVOICE` để ô trạng thái nói đúng thứ tab đếm.
 *
 * Hàm thuần, không React — bài test ghim từng nhánh vì cột này là thứ người
 * dùng đọc trước nhất trên 200 dòng.
 */

import type { StatusTone } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import { isPositiveAmount } from '@/features/mua-hang/purchase-row-status'
import {
  VOUCHER_STATUS_CANCELLED,
  VOUCHER_STATUS_DRAFT,
  VOUCHER_STATUS_POSTED,
} from '@/features/so-sach-thue/voucher-status'

import type { SalesInvoiceListItem } from './use-sales-invoices'

/** Khớp `SalesInvoiceKind` phía server. */
export const SALES_KIND_GOODS = 0
export const SALES_KIND_SERVICE = 1
export const SALES_KIND_RETURN = 2
export const SALES_KIND_ALLOWANCE = 3
export const SALES_KIND_AGENCY = 4
export const SALES_KIND_ADJUSTMENT_INCREASE = 5
export const SALES_KIND_ADJUSTMENT_DECREASE = 6

/** Nhãn của bảy loại chứng từ bán — tiêu đề form đọc từ đây. */
export const SALES_KIND_LABEL_KEYS = {
  [SALES_KIND_GOODS]: 'sales.kind.goods',
  [SALES_KIND_SERVICE]: 'sales.kind.service',
  [SALES_KIND_RETURN]: 'sales.kind.return',
  [SALES_KIND_ALLOWANCE]: 'sales.kind.allowance',
  [SALES_KIND_AGENCY]: 'sales.kind.agency',
  [SALES_KIND_ADJUSTMENT_INCREASE]: 'sales.kind.adjustmentIncrease',
  [SALES_KIND_ADJUSTMENT_DECREASE]: 'sales.kind.adjustmentDecrease',
} as const

/**
 * Năm loại người dùng tự lập từ nút "Tạo" (quyết định user 2026-09-18). Hai
 * loại điều chỉnh chỉ tới qua luồng hỏi–đáp sai sót (7H-3) bằng deep-link
 * `?kind=5|6&adjusts_voucher_id=`.
 */
export const SALES_CREATE_KINDS: readonly number[] = [
  SALES_KIND_GOODS,
  SALES_KIND_SERVICE,
  SALES_KIND_RETURN,
  SALES_KIND_ALLOWANCE,
  SALES_KIND_AGENCY,
]

/** Khớp `REVERSING_KINDS` phía server — ghi giảm doanh thu, bắt buộc đối trừ hóa đơn gốc. */
export const SALES_REVERSING_KINDS: readonly number[] = [
  SALES_KIND_RETURN,
  SALES_KIND_ALLOWANCE,
  SALES_KIND_ADJUSTMENT_DECREASE,
]

/** Khớp `ADJUSTMENT_KINDS` phía server — bắt buộc `adjusts_voucher_id`. */
export const SALES_ADJUSTMENT_KINDS: readonly number[] = [
  SALES_KIND_ADJUSTMENT_INCREASE,
  SALES_KIND_ADJUSTMENT_DECREASE,
]

/** Khớp `KINDS_NEEDING_EINVOICE` phía server — thiếu tờ HĐĐT là một VIỆC. */
export const SALES_KINDS_NEEDING_EINVOICE: readonly number[] = [
  SALES_KIND_GOODS,
  SALES_KIND_SERVICE,
  SALES_KIND_AGENCY,
  SALES_KIND_ADJUSTMENT_INCREASE,
  SALES_KIND_ADJUSTMENT_DECREASE,
]

export type SalesNextAction = 'post' | 'issue-einvoice' | 'collect' | null

export interface SalesRowStatus {
  readonly tone: StatusTone
  readonly label: string
  readonly nextAction: SalesNextAction
  /** Nhãn nút ở ô "Việc tiếp theo"; `null` khi không còn việc. */
  readonly actionLabel: string | null
}

/** Chứng từ đã ghi sổ mà thiếu tờ HĐĐT còn sống — đúng nhóm `chua-co-hoa-don`. */
export function isMissingEInvoice(row: SalesInvoiceListItem): boolean {
  return (
    row.status === VOUCHER_STATUS_POSTED &&
    SALES_KINDS_NEEDING_EINVOICE.includes(row.kind) &&
    !row.has_live_einvoice
  )
}

export function salesRowStatus(t: Translate, row: SalesInvoiceListItem): SalesRowStatus {
  if (row.status === VOUCHER_STATUS_DRAFT) {
    return {
      tone: 'todo',
      label: t('sales.list.status.draft'),
      nextAction: 'post',
      actionLabel: t('sales.list.action.post'),
    }
  }
  if (row.status === VOUCHER_STATUS_CANCELLED) {
    return { tone: 'bad', label: t('sales.list.status.cancelled'), nextAction: null, actionLabel: null }
  }
  const owed = isPositiveAmount(row.remaining_fc)
  if (owed && row.days_overdue !== null && row.days_overdue !== undefined) {
    return {
      tone: 'bad',
      label: t('sales.list.status.overdue', { days: String(row.days_overdue) }),
      nextAction: 'collect',
      actionLabel: t('sales.list.action.collectNow'),
    }
  }
  if (isMissingEInvoice(row)) {
    return {
      tone: 'bad',
      label: t('sales.list.status.missingInvoice'),
      nextAction: 'issue-einvoice',
      actionLabel: t('sales.list.action.issueInvoice'),
    }
  }
  if (owed) {
    return {
      tone: 'ok',
      label: t('sales.list.status.posted'),
      nextAction: 'collect',
      actionLabel: t('sales.list.action.collect'),
    }
  }
  return { tone: 'ok', label: t('sales.list.status.done'), nextAction: null, actionLabel: null }
}

/**
 * Ô "Hóa đơn": tờ HĐĐT còn sống đứng trước (ký hiệu · số; số chưa về khi tờ
 * đang truyền → "đang cấp số"), rồi tới bốn mảnh gõ tay; chứng từ thiếu hóa
 * đơn → lời "chưa có" và chỗ gọi tô đỏ; loại không cần hóa đơn mà trống → "—".
 */
export function invoiceText(
  t: Translate,
  row: SalesInvoiceListItem,
): { readonly text: string; readonly missing: boolean } {
  if (row.has_live_einvoice) {
    const parts = [row.einvoice_serial, row.einvoice_no].filter(
      (part): part is string => part !== null && part !== undefined && part.trim() !== '',
    )
    return {
      text: row.einvoice_no === null ? t('sales.list.invoice.numbering') : parts.join(' '),
      missing: false,
    }
  }
  const manual = [row.invoice_form, row.invoice_serial, row.invoice_no].filter(
    (part): part is string => part !== null && part !== undefined && part.trim() !== '',
  )
  if (manual.length > 0) {
    return { text: manual.join(' '), missing: false }
  }
  if (isMissingEInvoice(row)) {
    return { text: t('sales.list.invoice.notYet'), missing: true }
  }
  return { text: '—', missing: false }
}
