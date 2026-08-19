/**
 * Bảng cân đối tài khoản (`/so-sach-thue/bang-can-doi-tai-khoan`) — dư đầu,
 * phát sinh, dư cuối của một (năm tài chính, kỳ, sổ). `stale=true` là số tính
 * thẳng từ sổ cái vì snapshot chưa tính lại — băng cảnh báo, không phải lỗi.
 *
 * Không có drill-down (mở một dòng để xem phát sinh chi tiết) — đó là phase
 * 10a (U10), ngoài phạm vi lát này (YAGNI).
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { DataTableColumn, SelectOption } from '@/design-system/components'
import { Alert, DataTable, Seg, SelectField } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import { useFiscalYears } from './use-fiscal-years'
import { useTrialBalance, type TrialBalanceRow } from './use-trial-balance'

const LEDGER_FINANCIAL = '0'
const LEDGER_MANAGEMENT = '1'

export function TrialBalancePage(): ReactElement {
  const { t, locale } = useI18n()
  const fiscalYears = useFiscalYears()

  const [fiscalYearId, setFiscalYearId] = useState<number | null>(null)
  const [periodId, setPeriodId] = useState<number | null>(null)
  const [ledger, setLedger] = useState(LEDGER_FINANCIAL)

  // Chọn năm mới nhất + kỳ đầu của năm đó khi dữ liệu vừa tải xong — "điều
  // chỉnh state trong thân render" (khuyến nghị của React) thay vì effect: chỉ
  // MỘT lần (chặn bằng `fiscalYearId !== null`), không ghi đè lựa chọn người
  // dùng đã tự đổi sau đó.
  if (fiscalYearId === null && fiscalYears.data !== undefined && fiscalYears.data.length > 0) {
    const latestYear = fiscalYears.data[0]
    if (latestYear !== undefined) {
      setFiscalYearId(latestYear.id)
      setPeriodId(latestYear.periods[0]?.id ?? null)
    }
  }

  const selectedYear = fiscalYears.data?.find((year) => year.id === fiscalYearId) ?? null
  const balance = useTrialBalance(periodId, Number(ledger))

  const yearOptions: SelectOption[] = (fiscalYears.data ?? []).map((year) => ({
    value: String(year.id),
    label: year.code,
  }))
  const periodOptions: SelectOption[] = (selectedYear?.periods ?? []).map((period) => ({
    value: String(period.id),
    label: `${t('trialBalance.periodLabel', { no: String(period.period_no) })} (${formatDate(period.start_date, locale)}–${formatDate(period.end_date, locale)})`,
  }))

  const balanceError =
    balance.error instanceof ApiError
      ? translateErrorCode(t, balance.error.errorCode)
      : balance.isError
        ? t('error.transport.unreachable')
        : null

  const columns: DataTableColumn<TrialBalanceRow>[] = [
    { key: 'code', header: t('trialBalance.column.code'), render: (row) => row.account_code },
    { key: 'name', header: t('trialBalance.column.name'), render: (row) => row.account_name },
    {
      key: 'openingDebit',
      header: t('trialBalance.column.openingDebit'),
      align: 'right',
      render: (row) => formatMoney(row.opening_debit, locale),
    },
    {
      key: 'openingCredit',
      header: t('trialBalance.column.openingCredit'),
      align: 'right',
      render: (row) => formatMoney(row.opening_credit, locale),
    },
    {
      key: 'periodDebit',
      header: t('trialBalance.column.periodDebit'),
      align: 'right',
      render: (row) => formatMoney(row.period_debit, locale),
    },
    {
      key: 'periodCredit',
      header: t('trialBalance.column.periodCredit'),
      align: 'right',
      render: (row) => formatMoney(row.period_credit, locale),
    },
    {
      key: 'closingDebit',
      header: t('trialBalance.column.closingDebit'),
      align: 'right',
      render: (row) => formatMoney(row.closing_debit, locale),
    },
    {
      key: 'closingCredit',
      header: t('trialBalance.column.closingCredit'),
      align: 'right',
      render: (row) => formatMoney(row.closing_credit, locale),
    },
  ]

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <h1 className="text-lg font-semibold text-primary">{t('trialBalance.title')}</h1>

        <div className="flex flex-wrap items-end gap-3">
          <SelectField
            label={t('trialBalance.fiscalYear')}
            value={fiscalYearId === null ? '' : String(fiscalYearId)}
            options={yearOptions}
            onChange={(event) => {
              const nextId = Number(event.target.value)
              const year = fiscalYears.data?.find((candidate) => candidate.id === nextId)
              setFiscalYearId(nextId)
              setPeriodId(year?.periods[0]?.id ?? null)
            }}
          />
          <SelectField
            label={t('trialBalance.period')}
            value={periodId === null ? '' : String(periodId)}
            options={periodOptions}
            disabled={selectedYear === null}
            onChange={(event) => {
              setPeriodId(Number(event.target.value))
            }}
          />
          <Seg
            label={t('trialBalance.ledger')}
            value={ledger}
            onChange={setLedger}
            options={[
              { value: LEDGER_FINANCIAL, label: t('trialBalance.ledger.financial') },
              { value: LEDGER_MANAGEMENT, label: t('trialBalance.ledger.management') },
            ]}
          />
        </div>

        {balance.data?.stale === true && <Alert tone="warning">{t('trialBalance.stale')}</Alert>}
        {balanceError !== null && <Alert tone="error">{balanceError}</Alert>}

        <DataTable
          caption={t('trialBalance.title')}
          columns={columns}
          rows={balance.data?.rows ?? []}
          rowKey={(row) => String(row.account_id)}
          emptyLabel={periodId === null ? t('trialBalance.noPeriod') : t('trialBalance.empty')}
          loading={balance.isPending && periodId !== null}
          loadingLabel={t('common.loading')}
          zebra
        />
      </section>
    </div>
  )
}
