/**
 * Khối "Đối trừ công nợ" dưới lưới chứng từ nghiệp vụ khác (7H-2b).
 *
 * Đối trừ của chứng từ GLE gắn vào DÒNG, không vào chứng từ (7C-3: bù trừ
 * 131 ↔ 331 chạm hai TK, phân loại lại đầu năm chạm nhiều đối tác) — nên khối
 * này là một mục con cho mỗi dòng chạm công nợ, mỗi mục một bảng
 * `SettlementSection` hỏi `/gl/journal-vouchers/open-invoices` cho đúng dòng ấy.
 * Bên THUẬN (Nợ với phải thu, Có với phải trả) tất toán khoản ứng trước, bên
 * NGƯỢC tất toán khoản nợ — server suy, client chỉ nói lại cho người dùng biết
 * vì sao bảng dưới đây liệt kê thứ nó liệt kê.
 *
 * Khóa số tiền: `${rowId}|${settlementKey}` — form giữ state, khối chỉ vẽ.
 */

import type { ReactElement } from 'react'

import { SettlementSection } from '@/features/tien-vao-tien-ra/settlement-section'
import type { OpenInvoicesQuery } from '@/features/tien-vao-tien-ra/use-open-invoices'
import { useI18n } from '@/lib/i18n'

import { ROW_KEY_SEPARATOR, settlesAdvance, type DebtLineRow } from './journal-debt-lines'

export function JournalSettlementBlock({
  lines,
  branchId,
  asOf,
  currencyCode,
  amounts,
  onAmountChange,
  disabled,
}: {
  readonly lines: readonly DebtLineRow[]
  readonly branchId: number | null
  readonly asOf: string
  /** Tiền tệ của chứng từ — lưới GLE không có cột tiền tệ theo dòng, dòng dùng tiền tệ chứng từ. */
  readonly currencyCode: string
  readonly amounts: Readonly<Record<string, string>>
  readonly onAmountChange: (key: string, value: string) => void
  readonly disabled: boolean
}): ReactElement | null {
  const { t } = useI18n()
  if (lines.length === 0 || branchId === null || asOf.trim() === '') {
    return null
  }
  return (
    <section
      aria-label={t('gl.settlement.title')}
      className="flex flex-col gap-3 rounded border border-border-default bg-background p-4"
    >
      <h2 className="text-sm font-semibold text-primary">{t('gl.settlement.title')}</h2>
      <p className="text-xs text-text-muted">{t('gl.settlement.hint')}</p>
      {lines.map((line) => {
        const query: OpenInvoicesQuery = {
          basePath: '/api/v1/gl/journal-vouchers',
          // `side` không dùng trên đường GLE (server suy từ bên dòng), giữ để
          // đúng hợp đồng chung của hook.
          side: 'receivable',
          partnerKind: line.partnerKind,
          partnerId: line.partnerId,
          branchId,
          asOf,
          accountId: line.accountId,
          onDebit: line.onDebit,
          currencyCode,
        }
        // Chuyển khóa theo dòng ⇄ khóa `settlementKey` của bảng con.
        const prefix = `${line.rowId}${ROW_KEY_SEPARATOR}`
        const rowAmounts: Record<string, string> = {}
        for (const [key, value] of Object.entries(amounts)) {
          if (key.startsWith(prefix)) {
            rowAmounts[key.slice(prefix.length)] = value
          }
        }
        return (
          <div key={line.rowId} className="flex flex-col gap-1 border-t border-border-default pt-2">
            <p className="text-sm text-text-default">
              <span className="font-semibold">{t('gl.settlement.line', { row: String(line.rowNumber) })}</span>
              {' · '}
              {line.accountCode} · {line.partnerLabel} ·{' '}
              {line.onDebit ? t('gl.line.header.debit') : t('gl.line.header.credit')} {line.amount}
              {' — '}
              <span className="text-text-muted">
                {settlesAdvance(line) ? t('gl.settlement.settlesAdvance') : t('gl.settlement.settlesDebt')}
              </span>
            </p>
            <SettlementSection
              query={query}
              amounts={rowAmounts}
              onAmountChange={(key, value) => {
                onAmountChange(`${prefix}${key}`, value)
              }}
              disabled={disabled}
            />
          </div>
        )
      })}
    </section>
  )
}
