/**
 * Cột phải của form hóa đơn mua (design `#mua-form`): thẻ tổng (Tiền hàng ·
 * Chi phí mua · Thuế GTGT · **Phải trả NCC**), thẻ Thanh toán (hạn trả), thẻ
 * "Ghi sổ xong sẽ tạo ra" (bốn ô, hai ô nét đứt cho việc chưa/không xảy ra).
 *
 * Bốn con số của thẻ tổng là của SERVER (`total_*_fc` trên thân hóa đơn) —
 * client không cộng tiền (H15/LD-03), nên chứng từ chưa cất hiện "tính khi
 * cất" thay vì một tổng nhân chia bằng float. Đây là điểm lệch có chủ đích so
 * với design (tổng nhảy theo từng dòng gõ).
 */

import type { ReactElement, ReactNode } from 'react'

import { formatDate, formatMoney } from '@/lib/formatters'
import { useI18n } from '@/lib/i18n'

import { FormCard } from './form-card'
import { PURCHASE_KIND_RETURN } from './purchase-row-status'
import type { PurchaseInvoiceOut } from './use-purchase-invoices'

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

export function PurchaseSummaryCards({
  voucher,
  kind,
  dueDate,
  currencyCode,
  settlementTotal,
}: {
  readonly voucher: PurchaseInvoiceOut | null
  readonly kind: number
  /** Hạn trả đang khai trên form (trống = theo điều khoản NCC, server điền). */
  readonly dueDate: string
  readonly currencyCode: string
  /** Khối đối trừ (trả lại hàng) — chỗ gọi truyền câu tóm tắt, thẻ không cộng. */
  readonly settlementTotal: ReactNode
}): ReactElement {
  const { t, locale } = useI18n()
  const suffix = currencyCode.trim() === '' || currencyCode === 'VND' ? '' : ` ${currencyCode}`
  const money = (value: string): string => `${formatMoney(value, locale)}${suffix}`
  const pending = t('purchase.summary.pending')
  const isReturn = kind === PURCHASE_KIND_RETURN

  return (
    <aside className="flex w-full flex-col gap-4 lg:w-[290px] lg:shrink-0">
      <FormCard title={t('purchase.summary.title')}>
        <div className="flex flex-col gap-2 p-3.5">
          <Row label={t('purchase.summary.goods')} value={voucher === null ? pending : money(voucher.total_before_tax_fc)} />
          <Row label={t('purchase.summary.landedCost')} value={voucher === null ? pending : money(voucher.total_landed_cost_fc)} />
          <Row label={t('purchase.summary.vat')} value={voucher === null ? pending : money(voucher.total_vat_fc)} />
          <hr className="my-0.5 h-0.5 border-0 bg-primary" />
          <Row
            label={isReturn ? t('purchase.summary.returnTotal') : t('purchase.summary.payable')}
            value={voucher === null ? pending : money(voucher.total_fc)}
            strong
          />
        </div>
      </FormCard>

      <FormCard title={t('purchase.summary.payment.title')}>
        <div className="flex flex-col gap-2 p-3.5 text-sm">
          {isReturn ? (
            <p className="text-text-muted">{settlementTotal}</p>
          ) : (
            <>
              <p className="text-text-default">{t('purchase.summary.payment.later')}</p>
              <p className="text-xs text-text-muted">
                {dueDate.trim() === ''
                  ? voucher?.due_date === null || voucher?.due_date === undefined
                    ? t('purchase.summary.payment.dueByTerm')
                    : t('purchase.summary.payment.dueOn', { date: formatDate(voucher.due_date, locale) })
                  : t('purchase.summary.payment.dueOn', { date: formatDate(dueDate, locale) })}
              </p>
            </>
          )}
        </div>
      </FormCard>

      <FormCard title={t('purchase.summary.outcome.title')}>
        <div className="flex flex-col gap-3 p-3.5">
          <Outcome
            title={isReturn ? t('purchase.summary.outcome.return') : t('purchase.summary.outcome.invoice')}
            detail={
              voucher === null
                ? pending
                : isReturn
                  ? t('purchase.summary.outcome.returnDetail', { amount: money(voucher.total_fc) })
                  : t('purchase.summary.outcome.invoiceDetail', { amount: money(voucher.total_fc) })
            }
          />
          <Outcome
            title={t('purchase.summary.outcome.vat')}
            detail={voucher === null ? pending : t('purchase.summary.outcome.vatDetail', { amount: money(voucher.total_vat_fc) })}
          />
          <Outcome title={t('purchase.summary.outcome.stock')} detail={t('purchase.summary.outcome.stockDetail')} dashed />
          <Outcome title={t('purchase.summary.outcome.payment')} detail={t('purchase.summary.outcome.paymentDetail')} dashed />
        </div>
      </FormCard>
    </aside>
  )
}
