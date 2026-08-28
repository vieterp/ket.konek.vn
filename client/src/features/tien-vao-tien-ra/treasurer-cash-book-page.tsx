/**
 * Sổ quỹ tiền mặt của THỦ QUỸ (FR-WHK-005, bước 21) — dòng thô theo ngày ghi
 * sổ, phân trang `limit`/`offset` (nợ 6C, API 6E-1).
 *
 * Cố ý đọc `treasurer_cash_book` chứ không phải sổ kế toán: hai nguồn tách để
 * chênh lệch (phiếu chưa ghi sổ quỹ) không tự biến mất — báo cáo lệch là
 * `chenh-lech-so-quy-so-ke-toan` bên màn Báo cáo. Bản in có số tồn lũy kế là
 * mã mẫu S07-DN (lát 6E-1); màn này để tra nhanh trong ngày.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { DataTableColumn, LookupOption } from '@/design-system/components'
import { Alert, Button, DataTable, LookupInput, TextField } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError } from '@/lib/session'

import { todayIso } from '@/features/so-sach-thue/local-date'
import { useAccountLookup } from '@/features/so-sach-thue/use-account-lookup'

import { FeatureNav } from './feature-nav'
import {
  TREASURER_BOOK_PAGE_SIZE,
  useTreasurerCashBook,
  type TreasurerCashBookRow,
} from './use-treasurer'

const CASH_ACCOUNT_PREFIX = '111'

export function TreasurerCashBookPage(): ReactElement {
  const { t, locale } = useI18n()

  const [cashAccount, setCashAccount] = useState<LookupOption | null>(null)
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [page, setPage] = useState(1)

  const accountLookup = useAccountLookup(todayIso())
  const cashAccountOptions: LookupOption[] = [...accountLookup.maps.byCode.values()]
    .filter((account) => account.code.startsWith(CASH_ACCOUNT_PREFIX) && !account.is_summary)
    .map((account) => ({ id: account.id, code: account.code, label: account.name }))

  const book = useTreasurerCashBook({
    cashAccountId: cashAccount?.id,
    fromDate,
    toDate,
    page,
  })

  const rows = book.data?.items ?? []
  const total = book.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / TREASURER_BOOK_PAGE_SIZE))

  const columns: DataTableColumn<TreasurerCashBookRow>[] = [
    {
      key: 'book_date',
      header: t('cashflow.treasurerBook.column.date'),
      render: (row) => formatDate(row.book_date, locale),
    },
    {
      key: 'cash_account',
      header: t('cashflow.treasurerBook.column.account'),
      render: (row) => accountLookup.maps.byId.get(row.cash_account_id)?.code ?? '',
    },
    {
      key: 'receipt_amount',
      header: t('cashflow.treasurerBook.column.receipt'),
      align: 'right',
      render: (row) =>
        Number.parseFloat(row.receipt_amount) > 0 ? formatMoney(row.receipt_amount, locale) : '',
    },
    {
      key: 'payment_amount',
      header: t('cashflow.treasurerBook.column.payment'),
      align: 'right',
      render: (row) =>
        Number.parseFloat(row.payment_amount) > 0 ? formatMoney(row.payment_amount, locale) : '',
    },
  ]

  const listError =
    book.error instanceof ApiError
      ? translateErrorCode(t, book.error.errorCode)
      : book.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <h1 className="text-lg font-semibold text-primary">{t('cashflow.treasurerBook.title')}</h1>

        <div className="flex flex-wrap items-end gap-3">
          <LookupInput
            label={t('cashflow.treasurerBook.account')}
            value={cashAccount}
            onChange={(value) => {
              setCashAccount(value)
              setPage(1)
            }}
            options={cashAccountOptions}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('cashflow.form.lookupEmpty')}
          />
          <TextField
            label={t('cashflow.filter.fromDate')}
            type="date"
            value={fromDate}
            onChange={(event) => {
              setFromDate(event.target.value)
              setPage(1)
            }}
          />
          <TextField
            label={t('cashflow.filter.toDate')}
            type="date"
            value={toDate}
            onChange={(event) => {
              setToDate(event.target.value)
              setPage(1)
            }}
          />
        </div>

        {listError !== null && <Alert tone="error">{listError}</Alert>}

        <DataTable
          caption={t('cashflow.treasurerBook.title')}
          columns={columns}
          rows={rows}
          rowKey={(row) => String(row.id)}
          emptyLabel={t('cashflow.treasurerBook.empty')}
          loading={book.isPending}
          loadingLabel={t('common.loading')}
          zebra
        />

        <footer className="flex items-center justify-end gap-2 text-xs text-text-muted">
          <span>{t('cashflow.treasurerBook.total', { total: String(total) })}</span>
          <Button
            variant="ghost"
            disabled={page <= 1}
            onClick={() => {
              setPage((current) => Math.max(1, current - 1))
            }}
          >
            {t('cashflow.grid.prev')}
          </Button>
          <Button
            variant="ghost"
            disabled={page >= pageCount}
            onClick={() => {
              setPage((current) => current + 1)
            }}
          >
            {t('cashflow.grid.next')}
          </Button>
        </footer>
      </section>
    </div>
  )
}
