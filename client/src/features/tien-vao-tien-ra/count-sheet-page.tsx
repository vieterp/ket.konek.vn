/**
 * Màn Kiểm kê quỹ (FR-QUY-030/031): danh sách biên bản + drawer lập biên bản
 * mới + nút In (08a-TT) + nút sinh phiếu xử lý chênh lệch.
 *
 * Chênh lệch hiển thị theo chiều `thực tế − sổ` (dương = THỪA) — cùng chiều
 * `create_adjustment` phía server; bản in 08a-TT tự đảo chiều theo định nghĩa
 * của biểu mẫu (bẫy đã ghim ở 6E-2, client không tính lại).
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { DataTableColumn, LookupOption } from '@/design-system/components'
import { Alert, Button, DataTable, Drawer, LookupInput, TextField } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useAccess } from '@/lib/access'
import { JournalViolationsAlert } from '@/features/so-sach-thue/journal-violations-alert'
import { extractViolations, type Violation } from '@/features/so-sach-thue/journal-violations'
import { todayIso } from '@/features/so-sach-thue/local-date'
import { useAccountLookup } from '@/features/so-sach-thue/use-account-lookup'

import { FeatureNav } from './feature-nav'
import {
  COUNT_SHEET_PAGE_SIZE,
  useCountSheets,
  useCreateCountSheet,
  useCreateCountSheetAdjustment,
  usePrintCountSheet,
  type CountSheet,
} from './use-count-sheets'

const CASH_ACCOUNT_PREFIX = '111'

export function CountSheetPage(): ReactElement {
  const { t, locale } = useI18n()
  const { readOnly } = useSession()
  const access = useAccess()

  const [page, setPage] = useState(1)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [violations, setViolations] = useState<readonly Violation[]>([])
  const [pendingAdjustmentId, setPendingAdjustmentId] = useState<string | null>(null)

  const list = useCountSheets(page)
  const adjustment = useCreateCountSheetAdjustment()
  const print = usePrintCountSheet()

  // Bản đồ TK để hiện mã TK quỹ trên danh sách — biên bản chỉ mang id.
  const accountLookup = useAccountLookup(
    todayIso(),
    (list.data?.items ?? []).map((sheet) => sheet.cash_account_id),
  )

  function fail(caught: unknown, sheetId: string | null = null): void {
    setPendingAdjustmentId(sheetId)
    if (caught instanceof ApiError) {
      setError(translateErrorCode(t, caught.errorCode))
      setViolations(extractViolations(caught.problem))
    } else {
      setError(t('error.transport.unreachable'))
      setViolations([])
    }
  }

  function createAdjustment(sheetId: string, acknowledgeWarnings = false): void {
    setError(null)
    setViolations([])
    setPendingAdjustmentId(null)
    adjustment.mutate(
      { sheetId, idempotencyKey: newIdempotencyKey(), acknowledgeWarnings },
      {
        onError: (caught) => {
          fail(caught, sheetId)
        },
      },
    )
  }

  const rows = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / COUNT_SHEET_PAGE_SIZE))

  const columns: DataTableColumn<CountSheet>[] = [
    {
      key: 'count_date',
      header: t('cashflow.count.column.date'),
      render: (row) => formatDate(row.count_date, locale),
    },
    {
      key: 'cash_account',
      header: t('cashflow.count.column.account'),
      render: (row) => accountLookup.maps.byId.get(row.cash_account_id)?.code ?? '',
    },
    {
      key: 'book_balance',
      header: t('cashflow.count.column.bookBalance'),
      align: 'right',
      render: (row) => formatMoney(row.book_balance, locale),
    },
    {
      key: 'counted_total',
      header: t('cashflow.count.column.countedTotal'),
      align: 'right',
      render: (row) => formatMoney(row.counted_total, locale),
    },
    {
      key: 'difference',
      header: t('cashflow.count.column.difference'),
      align: 'right',
      render: (row) => (
        <span className={row.difference.startsWith('-') ? 'text-red-700' : undefined}>
          {formatMoney(row.difference, locale)}
        </span>
      ),
    },
    {
      key: 'actions',
      header: t('cashflow.count.column.actions'),
      render: (row) => (
        <span className="flex flex-wrap gap-2">
          <Button
            variant="ghost"
            disabled={print.isPending}
            onClick={() => {
              print.mutate(
                { sheetId: row.id, templateCode: null },
                {
                  onError: (caught) => {
                    fail(caught)
                  },
                },
              )
            }}
          >
            {t('cashflow.count.print')}
          </Button>
          {!readOnly &&
            row.adjustment_voucher_id === null &&
            row.difference !== '0' &&
            row.difference !== '0.00' && (
              <Button
                variant="secondary"
                disabled={adjustment.isPending}
                onClick={() => {
                  createAdjustment(row.id)
                }}
              >
                {t('cashflow.count.createAdjustment')}
              </Button>
            )}
        </span>
      ),
    },
  ]

  const listError =
    list.error instanceof ApiError
      ? translateErrorCode(t, list.error.errorCode)
      : list.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t('cashflow.count.title')}</h1>
          {!readOnly && (
            <Button
              onClick={() => {
                setDrawerOpen(true)
              }}
            >
              {t('cashflow.count.create')}
            </Button>
          )}
        </header>

        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {error !== null && (
          <JournalViolationsAlert
            error={error}
            violations={violations}
            busy={adjustment.isPending}
            onAcknowledge={
              pendingAdjustmentId === null
                ? undefined
                : () => {
                    createAdjustment(pendingAdjustmentId, true)
                  }
            }
          />
        )}

        <DataTable
          caption={t('cashflow.count.title')}
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          emptyLabel={t('cashflow.count.empty')}
          loading={list.isPending}
          loadingLabel={t('common.loading')}
          zebra
        />
        <footer className="flex items-center justify-end gap-2 text-xs text-text-muted">
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

        <CountSheetDrawer
          open={drawerOpen}
          onClose={() => {
            setDrawerOpen(false)
          }}
          branchId={access.data?.acting_branch_id ?? null}
        />
      </section>
    </div>
  )
}

function CountSheetDrawer({
  open,
  onClose,
  branchId,
}: {
  readonly open: boolean
  readonly onClose: () => void
  readonly branchId: number | null
}): ReactElement | null {
  const { t } = useI18n()
  const [countDate, setCountDate] = useState(todayIso)
  const [cashAccount, setCashAccount] = useState<LookupOption | null>(null)
  const [countedTotal, setCountedTotal] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)

  const accountLookup = useAccountLookup(countDate)
  const create = useCreateCountSheet()

  const cashAccountOptions: LookupOption[] = [...accountLookup.maps.byCode.values()]
    .filter((account) => account.code.startsWith(CASH_ACCOUNT_PREFIX) && !account.is_summary)
    .map((account) => ({ id: account.id, code: account.code, label: account.name }))

  if (!open) {
    return null
  }

  function handleSave(): void {
    setError(null)
    if (cashAccount === null) {
      setError(t('cashflow.count.error.accountRequired'))
      return
    }
    if (countedTotal.trim() === '') {
      setError(t('cashflow.count.error.countedTotalRequired'))
      return
    }
    if (branchId === null) {
      setError(t('cashflow.form.branchMissing'))
      return
    }
    create.mutate(
      {
        body: {
          branch_id: branchId,
          cash_account_id: cashAccount.id,
          count_date: countDate,
          counted_total: countedTotal.trim(),
          lines: [],
          ...(note.trim() === '' ? {} : { note: note.trim() }),
        },
        idempotencyKey,
      },
      {
        onSuccess: () => {
          // Khóa chống trùng thuộc TỪNG biên bản — đổi khóa cho lần lập sau.
          setIdempotencyKey(newIdempotencyKey())
          setCountedTotal('')
          setNote('')
          onClose()
        },
        onError: (caught) => {
          setError(
            caught instanceof ApiError
              ? translateErrorCode(t, caught.errorCode)
              : t('error.transport.unreachable'),
          )
        },
      },
    )
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={t('cashflow.count.create')}
      closeLabel={t('common.close')}
    >
      <div className="flex flex-col gap-3">
        {error !== null && <Alert tone="error">{error}</Alert>}
        <TextField
          label={t('cashflow.count.column.date')}
          type="date"
          value={countDate}
          onChange={(event) => {
            setCountDate(event.target.value)
          }}
        />
        <LookupInput
          label={t('cashflow.count.column.account')}
          value={cashAccount}
          onChange={setCashAccount}
          options={cashAccountOptions}
          clearLabel={t('catalog.lookup.clear')}
          emptyLabel={t('cashflow.form.lookupEmpty')}
        />
        <TextField
          label={t('cashflow.count.column.countedTotal')}
          inputMode="decimal"
          value={countedTotal}
          onChange={(event) => {
            setCountedTotal(event.target.value)
          }}
        />
        <TextField
          label={t('cashflow.count.note')}
          value={note}
          onChange={(event) => {
            setNote(event.target.value)
          }}
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button disabled={create.isPending} onClick={handleSave}>
            {create.isPending ? t('cashflow.form.saving') : t('cashflow.count.save')}
          </Button>
        </div>
      </div>
    </Drawer>
  )
}
