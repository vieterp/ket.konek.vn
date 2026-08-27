/**
 * Khối đối trừ công nợ trên form phiếu/chứng từ ngân hàng (`docs/srs/03` §4):
 * chọn đối tác xong, khối này liệt kê chứng từ còn nợ và nhận "Số đối trừ"
 * từng dòng. Số VND và chênh lệch tỷ giá do SERVER tính (FR-SYS-066) — client
 * không cộng trừ gì ngoài một dòng tổng hiển thị để người dùng tự soát
 * BR-QUY-03 (tổng đối trừ = tổng phiếu) trước khi server phán.
 */

import type { ReactElement } from 'react'

import { Alert } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError } from '@/lib/session'

import { useOpenInvoices, type OpenInvoicesQuery } from './use-open-invoices'

/** Khóa một dòng đối trừ trong state của form: `${target_kind}:${target_id}`. */
export function settlementKey(targetKind: number, targetId: string): string {
  return `${String(targetKind)}:${targetId}`
}

export function SettlementSection({
  query,
  amounts,
  onAmountChange,
  disabled,
}: {
  readonly query: OpenInvoicesQuery | null
  /** Số nguyên tệ người dùng gõ, khóa theo `settlementKey` — form giữ state. */
  readonly amounts: Readonly<Record<string, string>>
  readonly onAmountChange: (key: string, value: string) => void
  readonly disabled: boolean
}): ReactElement | null {
  const { t, locale } = useI18n()
  const invoices = useOpenInvoices(query)

  if (query === null) {
    return null
  }

  const errorMessage =
    invoices.error instanceof ApiError
      ? translateErrorCode(t, invoices.error.errorCode)
      : invoices.isError
        ? t('error.transport.unreachable')
        : null

  const items = invoices.data?.items ?? []

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold text-primary">{t('cashflow.settlement.title')}</h2>
      {errorMessage !== null && <Alert tone="error">{errorMessage}</Alert>}
      {invoices.isPending ? (
        <p className="text-app text-text-muted">{t('common.loading')}</p>
      ) : items.length === 0 ? (
        <p className="text-app text-text-muted">{t('cashflow.settlement.empty')}</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <caption className="sr-only">{t('cashflow.settlement.title')}</caption>
          <thead>
            <tr className="border-b border-border-default text-left text-xs text-text-muted">
              <th scope="col" className="py-1 pr-2">
                {t('cashflow.settlement.column.invoiceNo')}
              </th>
              <th scope="col" className="py-1 pr-2">
                {t('cashflow.settlement.column.invoiceDate')}
              </th>
              <th scope="col" className="py-1 pr-2 text-right">
                {t('cashflow.settlement.column.remaining')}
              </th>
              <th scope="col" className="py-1 text-right">
                {t('cashflow.settlement.column.amount')}
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((invoice) => {
              const key = settlementKey(invoice.target_kind, invoice.target_id)
              return (
                <tr key={key} className="border-b border-border-default">
                  <td className="py-1 pr-2">{invoice.invoice_no}</td>
                  <td className="py-1 pr-2">{formatDate(invoice.invoice_date, locale)}</td>
                  <td className="py-1 pr-2 text-right">
                    {formatMoney(invoice.remaining_fc, locale)}
                    {invoice.currency_code !== 'VND' ? ` ${invoice.currency_code}` : ''}
                  </td>
                  <td className="py-1 text-right">
                    <input
                      aria-label={t('cashflow.settlement.amountLabel', {
                        no: invoice.invoice_no,
                      })}
                      inputMode="decimal"
                      disabled={disabled}
                      value={amounts[key] ?? ''}
                      onChange={(event) => {
                        onAmountChange(key, event.target.value)
                      }}
                      className="w-32 rounded border border-border-default px-2 py-1 text-right text-sm text-text-default outline-none focus:border-ocean-500 focus:ring-2 focus:ring-ocean-200"
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}
