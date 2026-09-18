/**
 * Cột phải của form hóa đơn bán (bộ xương `#mua-form` dùng lại): thẻ tổng
 * (Tiền hàng · Chiết khấu · Thuế GTGT · **Phải thu khách**), thẻ Thanh toán
 * (hạn thu), thẻ "Ghi sổ xong sẽ tạo ra" (bốn ô; HĐĐT và xuất kho nét đứt —
 * việc của bước sau / phân hệ sau).
 *
 * Bốn con số của thẻ tổng là của SERVER (`total_*_fc` trên thân hóa đơn) —
 * client không cộng tiền (H15/LD-03), nên chứng từ chưa cất hiện "tính khi
 * cất" — cùng điểm lệch có chủ đích so với design như 7H-1.
 */

import type { ReactElement, ReactNode } from 'react'

import { formatDate, formatMoney } from '@/lib/formatters'
import { useI18n } from '@/lib/i18n'

import { FormCard } from '@/features/mua-hang/form-card'

import { SALES_REVERSING_KINDS } from './sales-row-status'
import type { SalesInvoiceOut } from './use-sales-invoices'

function Row({ label, value, strong = false }: { readonly label: string; readonly value: string; readonly strong?: boolean }): ReactElement {
  return (
    <div className="flex items-baseline justify-between gap-2 text-sm">
      <span className={strong ? 'font-semibold text-primary' : 'text-text-muted'}>{label}</span>
      <b className={`tabular-nums ${strong ? 'text-xl text-primary' : 'text-text-default'}`}>{value}</b>
    </div>
  )
}

function Outcome({
  title,
  detail,
  dashed = false,
}: {
  readonly title: string
  readonly detail: string
  readonly dashed?: boolean
}): ReactElement {
  return (
    <div
      className={`flex-1 border p-3 ${
        dashed ? 'border-dashed border-border-default text-text-muted' : 'border-border-default'
      }`}
    >
      <div className={`text-sm font-semibold ${dashed ? '' : 'text-primary'}`}>{title}</div>
      <div className="mt-1 text-xs text-text-muted">{detail}</div>
    </div>
  )
}

export function SalesSummaryCards({
  voucher,
  kind,
  dueDate,
  currencyCode,
  settlementTotal,
}: {
  readonly voucher: SalesInvoiceOut | null
  readonly kind: number
  /** Hạn thu đang khai trên form (trống = theo điều khoản khách, server điền). */
  readonly dueDate: string
  readonly currencyCode: string
  /** Khối đối trừ (giảm trừ) — chỗ gọi truyền câu tóm tắt, thẻ không cộng. */
  readonly settlementTotal: ReactNode
}): ReactElement {
  const { t, locale } = useI18n()
  const suffix = currencyCode.trim() === '' || currencyCode === 'VND' ? '' : ` ${currencyCode}`
  const money = (value: string): string => `${formatMoney(value, locale)}${suffix}`
  const pending = t('sales.summary.pending')
  const reversing = SALES_REVERSING_KINDS.includes(kind)

  return (
    <aside className="flex w-full flex-col gap-4 lg:w-[290px] lg:shrink-0">
      <FormCard title={t('sales.summary.title')}>
        <div className="flex flex-col gap-2 p-3.5">
          <Row label={t('sales.summary.goods')} value={voucher === null ? pending : money(voucher.total_before_tax_fc)} />
          <Row label={t('sales.summary.discount')} value={voucher === null ? pending : money(voucher.total_discount_fc)} />
          <Row label={t('sales.summary.vat')} value={voucher === null ? pending : money(voucher.total_vat_fc)} />
          <hr className="my-0.5 h-0.5 border-0 bg-primary" />
          <Row
            label={reversing ? t('sales.summary.reversingTotal') : t('sales.summary.receivable')}
            value={voucher === null ? pending : money(voucher.total_fc)}
            strong
          />
        </div>
      </FormCard>

      <FormCard title={t('sales.summary.payment.title')}>
        <div className="flex flex-col gap-2 p-3.5 text-sm">
          {reversing ? (
            <p className="text-text-muted">{settlementTotal}</p>
          ) : (
            <>
              <p className="text-text-default">{t('sales.summary.payment.later')}</p>
              <p className="text-xs text-text-muted">
                {dueDate.trim() === ''
                  ? voucher?.due_date === null || voucher?.due_date === undefined
                    ? t('sales.summary.payment.dueByTerm')
                    : t('sales.summary.payment.dueOn', { date: formatDate(voucher.due_date, locale) })
                  : t('sales.summary.payment.dueOn', { date: formatDate(dueDate, locale) })}
              </p>
            </>
          )}
        </div>
      </FormCard>

      <FormCard title={t('sales.summary.outcome.title')}>
        <div className="flex flex-col gap-3 p-3.5">
          <Outcome
            title={reversing ? t('sales.summary.outcome.reversing') : t('sales.summary.outcome.invoice')}
            detail={
              voucher === null
                ? pending
                : reversing
                  ? t('sales.summary.outcome.reversingDetail', { amount: money(voucher.total_fc) })
                  : t('sales.summary.outcome.invoiceDetail', { amount: money(voucher.total_fc) })
            }
          />
          <Outcome
            title={t('sales.summary.outcome.vat')}
            detail={voucher === null ? pending : t('sales.summary.outcome.vatDetail', { amount: money(voucher.total_vat_fc) })}
          />
          <Outcome title={t('sales.summary.outcome.einvoice')} detail={t('sales.summary.outcome.einvoiceDetail')} dashed />
          <Outcome title={t('sales.summary.outcome.stock')} detail={t('sales.summary.outcome.stockDetail')} dashed />
        </div>
      </FormCard>
    </aside>
  )
}
