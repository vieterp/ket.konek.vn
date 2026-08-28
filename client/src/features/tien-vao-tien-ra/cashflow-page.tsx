/**
 * Màn hình "Tiền vào tiền ra" (bước 19, design nhóm 03): MỘT trang cho cả quỹ
 * lẫn ngân hàng — hàng thẻ trên cùng, lưới giao dịch của thẻ đang chọn bên
 * dưới, nút lập chứng từ đổi theo ngữ cảnh thẻ.
 *
 * Backend vẫn là HAI module (`cash_book` + `bank`) — trang này chỉ đọc BFF
 * `/api/v1/cashflow/*`; mọi lệnh ghi đi qua form của từng loại chứng từ.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { DataTableColumn } from '@/design-system/components'
import { Alert, Button, DataTable, SelectField, StatusPill, TextField } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { voucherStatusLabel, voucherStatusTone } from '@/features/so-sach-thue/voucher-status'

import { AccountCards } from './account-cards'
import { resolveSelectedCard } from './selected-card'
import { FeatureNav } from './feature-nav'
import {
  TRANSACTION_PAGE_SIZE,
  useCashflowOverview,
  useCashflowTransactions,
  type CashflowTransaction,
  type SelectedCard,
} from './use-cashflow'

/** Mã loại chứng từ quỹ — dòng lưới thuộc hai mã này mở form phiếu thu/chi. */
const CASH_DOCUMENT_TYPES: readonly string[] = ['PT', 'PC']

function errorText(
  t: ReturnType<typeof useI18n>['t'],
  error: unknown,
  isError: boolean,
): string | null {
  if (error instanceof ApiError) {
    return translateErrorCode(t, error.errorCode)
  }
  return isError ? t('error.transport.unreachable') : null
}

export function CashflowPage(): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()

  const overview = useCashflowOverview()
  const [chosen, setChosen] = useState<SelectedCard | null>(null)
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [status, setStatus] = useState('')
  const [page, setPage] = useState(1)

  // Luật chọn thẻ (kể cả "thẻ đã chọn phải CÒN trong overview mới" — nợ L-4
  // review 6F-1) nằm ở hàm thuần `resolveSelectedCard`, có test riêng.
  const selected: SelectedCard | null = resolveSelectedCard(chosen, overview.data)

  const transactions = useCashflowTransactions(
    selected === null
      ? null
      : {
          card: selected,
          fromDate,
          toDate,
          ...(status === '' ? {} : { status: Number.parseInt(status, 10) }),
          page,
        },
  )

  function selectCard(card: SelectedCard): void {
    setChosen(card)
    setPage(1)
  }

  function openVoucher(row: CashflowTransaction): void {
    const base = '/tien-vao-tien-ra/giao-dich'
    const path = CASH_DOCUMENT_TYPES.includes(row.document_type)
      ? `${base}/phieu/${row.voucher_id}`
      : `${base}/ngan-hang/${row.voucher_id}`
    void navigate(path)
  }

  const rows = transactions.data?.items ?? []
  const total = transactions.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / TRANSACTION_PAGE_SIZE))

  const columns: DataTableColumn<CashflowTransaction>[] = [
    {
      key: 'voucher_no',
      header: t('cashflow.grid.column.no'),
      render: (row) => (
        <button
          type="button"
          className="text-secondary hover:underline"
          onClick={() => {
            openVoucher(row)
          }}
        >
          {row.voucher_no}
        </button>
      ),
    },
    {
      key: 'posting_date',
      header: t('cashflow.grid.column.postingDate'),
      align: 'right',
      render: (row) => formatDate(row.posting_date, locale),
    },
    {
      key: 'description',
      header: t('cashflow.grid.column.description'),
      render: (row) => row.description ?? '',
    },
    {
      key: 'partner_name',
      header: t('cashflow.grid.column.partner'),
      render: (row) => row.partner_name ?? '',
    },
    {
      key: 'amount',
      header: t('cashflow.grid.column.amount'),
      align: 'right',
      render: (row) => (
        <span className={row.amount.startsWith('-') ? 'text-red-700' : undefined}>
          {formatMoney(row.amount, locale)}
        </span>
      ),
    },
    {
      key: 'status',
      header: t('cashflow.grid.column.status'),
      render: (row) => (
        <StatusPill tone={voucherStatusTone(row.status)}>
          {voucherStatusLabel(t, row.status)}
        </StatusPill>
      ),
    },
  ]

  const overviewError = errorText(t, overview.error, overview.isError)
  const listError = errorText(t, transactions.error, transactions.isError)
  const bankContext = selected?.source === 'bank'

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t('cashflow.title')}</h1>
          {!readOnly && (
            <span className="flex flex-wrap gap-2">
              {bankContext ? (
                <>
                  <Button
                    onClick={() => {
                      void navigate('/tien-vao-tien-ra/giao-dich/ngan-hang/moi')
                    }}
                  >
                    {t('cashflow.action.createBank')}
                  </Button>
                </>
              ) : (
                <>
                  <Button
                    onClick={() => {
                      void navigate('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')
                    }}
                  >
                    {t('cashflow.action.createReceipt')}
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => {
                      void navigate('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=1')
                    }}
                  >
                    {t('cashflow.action.createPayment')}
                  </Button>
                </>
              )}
            </span>
          )}
        </header>

        {overviewError !== null && <Alert tone="error">{overviewError}</Alert>}

        {overview.isPending ? (
          <p className="text-app text-text-muted">{t('common.loading')}</p>
        ) : (
          <AccountCards
            cashAccounts={overview.data?.cash_accounts ?? []}
            bankAccounts={overview.data?.bank_accounts ?? []}
            unassignedDeposit={overview.data?.unassigned_deposit ?? '0'}
            selected={selected}
            onSelect={selectCard}
          />
        )}

        {selected !== null && (
          <>
            <div className="flex flex-wrap items-end gap-2">
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
              <SelectField
                label={t('cashflow.filter.status')}
                value={status}
                onChange={(event) => {
                  setStatus(event.target.value)
                  setPage(1)
                }}
                options={[
                  { value: '', label: t('cashflow.filter.statusAll') },
                  { value: '1', label: t('gl.voucher.status.draft') },
                  { value: '2', label: t('gl.voucher.status.posted') },
                ]}
              />
            </div>

            {listError !== null && <Alert tone="error">{listError}</Alert>}

            <DataTable
              caption={t('cashflow.grid.caption')}
              columns={columns}
              rows={rows}
              rowKey={(row) => row.voucher_id}
              emptyLabel={t('cashflow.grid.empty')}
              loading={transactions.isPending}
              loadingLabel={t('common.loading')}
              zebra
            />
            <footer className="flex items-center justify-between text-xs text-text-muted">
              <span>
                {t('cashflow.grid.pageInfo', {
                  from: String(total === 0 ? 0 : (page - 1) * TRANSACTION_PAGE_SIZE + 1),
                  to: String(Math.min(total, page * TRANSACTION_PAGE_SIZE)),
                  total: String(total),
                })}
              </span>
              <span className="flex gap-2">
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
              </span>
            </footer>
          </>
        )}
      </section>
    </div>
  )
}
