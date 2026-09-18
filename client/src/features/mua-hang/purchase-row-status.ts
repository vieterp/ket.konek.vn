/**
 * Trạng thái + việc tiếp theo của một dòng lưới chứng từ mua hàng (U1, màn 01).
 *
 * Trục ngữ nghĩa là "có việc cho bạn không" (docs/design-guidelines.md §5 ba
 * tông): chưa ghi sổ = `todo`; quá hạn / thiếu hóa đơn NCC = `bad`; đã ghi sổ
 * còn nợ chưa tới hạn và đã hết nợ đều là `ok` — xám, lùi khỏi tầm mắt.
 *
 * Hàm thuần, không React — bài test ghim từng nhánh vì cột này là thứ người
 * dùng đọc trước nhất trên 200 dòng.
 */

import type { StatusTone } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import {
  VOUCHER_STATUS_CANCELLED,
  VOUCHER_STATUS_DRAFT,
} from '@/features/so-sach-thue/voucher-status'

import type { PurchaseInvoiceListItem } from './use-purchase-invoices'

/** Khớp `VendorInvoiceStatus` phía server: 0 đã có · 1 chưa có (về sau) · 2 không có. */
export const VENDOR_INVOICE_RECEIVED = 0
export const VENDOR_INVOICE_NOT_YET = 1
export const VENDOR_INVOICE_NONE = 2

/** Khớp `PurchaseInvoiceKind` phía server. */
export const PURCHASE_KIND_GOODS = 0
export const PURCHASE_KIND_SERVICE = 1
export const PURCHASE_KIND_ASSET = 2
export const PURCHASE_KIND_IN_TRANSIT = 3
export const PURCHASE_KIND_RETURN = 4

/** Nhãn của năm loại chứng từ mua — nút "Tạo" và tiêu đề form đọc từ đây. */
export const PURCHASE_KIND_LABEL_KEYS = {
  [PURCHASE_KIND_GOODS]: 'purchase.kind.goods',
  [PURCHASE_KIND_SERVICE]: 'purchase.kind.service',
  [PURCHASE_KIND_ASSET]: 'purchase.kind.asset',
  [PURCHASE_KIND_IN_TRANSIT]: 'purchase.kind.inTransit',
  [PURCHASE_KIND_RETURN]: 'purchase.kind.return',
} as const

export type PurchaseNextAction = 'post' | 'add-invoice' | 'pay' | null

export interface PurchaseRowStatus {
  readonly tone: StatusTone
  readonly label: string
  readonly nextAction: PurchaseNextAction
  /** Nhãn nút ở ô "Việc tiếp theo"; `null` khi không còn việc. */
  readonly actionLabel: string | null
}

/**
 * Số tiền dạng chuỗi thập phân có dương không — so bằng ký tự, KHÔNG parse
 * thành `number` (H15: client không tính tiền, kể cả một phép so sánh qua float).
 */
export function isPositiveAmount(amount: string | null | undefined): boolean {
  return amount !== null && amount !== undefined && !amount.startsWith('-') && /[1-9]/.test(amount)
}

export function purchaseRowStatus(t: Translate, row: PurchaseInvoiceListItem): PurchaseRowStatus {
  if (row.status === VOUCHER_STATUS_DRAFT) {
    return {
      tone: 'todo',
      label: t('purchase.list.status.draft'),
      nextAction: 'post',
      actionLabel: t('purchase.list.action.post'),
    }
  }
  if (row.status === VOUCHER_STATUS_CANCELLED) {
    return { tone: 'bad', label: t('purchase.list.status.cancelled'), nextAction: null, actionLabel: null }
  }
  const owes = isPositiveAmount(row.remaining_fc)
  if (owes && row.days_overdue !== null && row.days_overdue !== undefined) {
    return {
      tone: 'bad',
      label: t('purchase.list.status.overdue', { days: String(row.days_overdue) }),
      nextAction: 'pay',
      actionLabel: t('purchase.list.action.payNow'),
    }
  }
  if (row.vendor_invoice_status === VENDOR_INVOICE_NOT_YET) {
    return {
      tone: 'bad',
      label: t('purchase.list.status.missingInvoice'),
      nextAction: 'add-invoice',
      actionLabel: t('purchase.list.action.addInvoice'),
    }
  }
  if (owes) {
    return {
      tone: 'ok',
      label: t('purchase.list.status.posted'),
      nextAction: 'pay',
      actionLabel: t('purchase.list.action.pay'),
    }
  }
  return { tone: 'ok', label: t('purchase.list.status.done'), nextAction: null, actionLabel: null }
}

/** Ô "Hóa đơn NCC": ba mảnh ghép lại, hoặc lời "chưa có"/"không có" — chỗ gọi tô đỏ khi `missing`. */
export function vendorInvoiceText(
  t: Translate,
  row: PurchaseInvoiceListItem,
): { readonly text: string; readonly missing: boolean } {
  if (row.vendor_invoice_status === VENDOR_INVOICE_NOT_YET) {
    return { text: t('purchase.list.vendorInvoice.notYet'), missing: true }
  }
  if (row.vendor_invoice_status === VENDOR_INVOICE_NONE) {
    return { text: t('purchase.list.vendorInvoice.none'), missing: false }
  }
  const parts = [row.vendor_invoice_form, row.vendor_invoice_serial, row.vendor_invoice_no].filter(
    (part): part is string => part !== null && part !== undefined && part.trim() !== '',
  )
  return { text: parts.join(' '), missing: false }
}
