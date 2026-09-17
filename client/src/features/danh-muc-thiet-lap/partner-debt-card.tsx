/**
 * Thẻ công nợ trên màn hình đối tác — nguyên tắc nhóm 07: "thẻ công nợ hiện
 * ngay" cạnh thông tin danh mục.
 *
 * Phase 3 vẽ khung giữ chỗ (H56); lát 7G-4 thay ruột bằng `debt` của BFF
 * `partners/{id}/overview`. Hai nửa phải thu / phải trả, mỗi nửa chỉ có mặt khi
 * người xem có quyền xem chứng từ của chiều đó — server trả `null`, và thẻ nói
 * thẳng "không có quyền xem" thay vì in một con số 0 giả. Số tiền là chuỗi
 * thập phân từ server, đi thẳng vào `formatMoney` — không qua `Number`.
 */

import type { ReactElement } from 'react'
import { Wallet } from 'lucide-react'

import { StatusPill } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { useI18n } from '@/lib/i18n'

import type { PartnerDebt } from './use-partner'

type DebtSide = NonNullable<PartnerDebt['receivable']>

interface PartnerDebtCardProps {
  readonly debt: PartnerDebt
}

function Amount({ label, value }: { readonly label: string; readonly value: string }): ReactElement {
  const { locale } = useI18n()
  return (
    <div className="flex justify-between gap-2 text-sm">
      <dt className="text-text-muted">{label}</dt>
      <dd className="tabular-nums text-text-default">{formatMoney(value, locale)}</dd>
    </div>
  )
}

function DebtSideBlock({
  title,
  side,
}: {
  readonly title: string
  readonly side: DebtSide | null
}): ReactElement {
  const { t, locale } = useI18n()
  const overdue = side !== null && side.overdue_count > 0
  return (
    <section aria-label={title} className="flex flex-col gap-1">
      <h3 className="flex items-center justify-between gap-2 text-sm font-semibold text-text-default">
        {title}
        {overdue && (
          <StatusPill tone="bad">
            {t('partner.debt.overdueCount', { count: String(side.overdue_count) })}
          </StatusPill>
        )}
      </h3>
      {side === null ? (
        <p className="text-sm text-text-muted">{t('partner.debt.noPermission')}</p>
      ) : (
        <dl className="flex flex-col gap-1">
          <Amount label={t('partner.debt.open')} value={side.open_amount} />
          <Amount label={t('partner.debt.overdue')} value={side.overdue_amount} />
          {side.oldest_due_date !== null && (
            <div className="flex justify-between gap-2 text-sm">
              <dt className="text-text-muted">{t('partner.debt.oldestDue')}</dt>
              <dd className="text-text-default">{formatDate(side.oldest_due_date, locale)}</dd>
            </div>
          )}
        </dl>
      )}
    </section>
  )
}

export function PartnerDebtCard({ debt }: PartnerDebtCardProps): ReactElement {
  const { t, locale } = useI18n()

  return (
    <section
      aria-label={t('partner.debt.title')}
      className="rounded border border-border-default bg-background p-4"
    >
      <h2 className="mb-2 flex items-center justify-between gap-2 text-sm font-semibold text-primary">
        <span className="flex items-center gap-2">
          <Wallet size={16} aria-hidden />
          {t('partner.debt.title')}
        </span>
        <span className="text-meta font-normal text-text-muted">
          {t('partner.debt.asOf', { date: formatDate(debt.as_of, locale) })}
        </span>
      </h2>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <DebtSideBlock title={t('partner.debt.receivable')} side={debt.receivable} />
        <DebtSideBlock title={t('partner.debt.payable')} side={debt.payable} />
      </div>
      {debt.credit_available !== null && (
        <dl className="mt-3 border-t border-border-default pt-2">
          <Amount label={t('partner.debt.creditAvailable')} value={debt.credit_available} />
        </dl>
      )}
    </section>
  )
}
