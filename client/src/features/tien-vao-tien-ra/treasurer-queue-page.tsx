/**
 * Màn thủ quỹ đúng 3 việc (bước 21, U6): nhận đề nghị (hàng đợi phiếu đã ghi
 * sổ kế toán, BR-WHK-01) → thu/chi tiền thật → Ghi sổ quỹ (chọn nhiều phiếu,
 * một transaction — FR-WHK-002).
 *
 * Ngày ghi sổ hai kiểu (FR-WHK-003): theo ngày chứng từ, hoặc MỘT ngày tùy
 * chọn cho cả lô — bị chặn dưới ngày hạch toán từng phiếu (BR-WHK-05) và trên
 * ngày hiện tại (sổ quỹ ghi việc ĐÃ làm — quyết định user 2026-08-27, ô ngày
 * cũng đặt `max` để chặn sớm; server vẫn là cổng chính).
 *
 * Không có nút "Từ chối": hoãn sau v1 (quyết định user 2026-08-27) — phiếu sai
 * đi đường bỏ-ghi-sổ/sửa của kế toán.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button, DataTable, Seg, TextField } from '@/design-system/components'
import type { DataTableColumn } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { todayIso } from '@/features/so-sach-thue/local-date'

import { FeatureNav } from './feature-nav'
import {
  useBookVouchers,
  useTreasurerQueue,
  type TreasurerQueueItem,
} from './use-treasurer'

export function TreasurerQueuePage(): ReactElement {
  const { t, locale } = useI18n()
  const { readOnly } = useSession()

  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [dateMode, setDateMode] = useState<'posting_date' | 'custom'>('posting_date')
  const [customDate, setCustomDate] = useState(todayIso)
  const [error, setError] = useState<string | null>(null)
  const [bookedMessage, setBookedMessage] = useState<string | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)

  const queue = useTreasurerQueue()
  const booker = useBookVouchers()

  const items = queue.data?.items ?? []
  const selectedCount = items.filter((item) => selected.has(item.voucher_id)).length
  const allSelected = items.length > 0 && selectedCount === items.length

  function toggle(voucherId: string): void {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(voucherId)) {
        next.delete(voucherId)
      } else {
        next.add(voucherId)
      }
      return next
    })
  }

  function handleBook(): void {
    setError(null)
    setBookedMessage(null)
    const voucherIds = items
      .map((item) => item.voucher_id)
      .filter((voucherId) => selected.has(voucherId))
    if (voucherIds.length === 0) {
      setError(t('cashflow.treasurer.error.nothingSelected'))
      return
    }
    if (dateMode === 'custom' && customDate.trim() === '') {
      setError(t('cashflow.treasurer.error.customDateRequired'))
      return
    }
    booker.mutate(
      {
        body: {
          voucher_ids: voucherIds,
          book_date_mode: dateMode,
          book_date: dateMode === 'custom' ? customDate : null,
        },
        idempotencyKey,
      },
      {
        onSuccess: (result) => {
          // Khóa chống trùng thuộc TỪNG lô — đổi khóa cho lượt ghi sau.
          setIdempotencyKey(newIdempotencyKey())
          setSelected(new Set())
          setBookedMessage(
            t('cashflow.treasurer.booked', { count: String(result.booked_count) }),
          )
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

  const columns: DataTableColumn<TreasurerQueueItem>[] = [
    {
      key: 'pick',
      header: t('cashflow.treasurer.column.pick'),
      render: (row) => (
        <input
          type="checkbox"
          aria-label={t('cashflow.treasurer.pickOne', { no: row.voucher_no })}
          checked={selected.has(row.voucher_id)}
          disabled={readOnly}
          onChange={() => {
            toggle(row.voucher_id)
          }}
        />
      ),
    },
    { key: 'voucher_no', header: t('cashflow.treasurer.column.voucherNo'), render: (row) => row.voucher_no },
    {
      key: 'posting_date',
      header: t('cashflow.treasurer.column.postingDate'),
      render: (row) => formatDate(row.posting_date, locale),
    },
    {
      key: 'direction',
      header: t('cashflow.treasurer.column.direction'),
      render: (row) =>
        row.is_receipt ? t('cashflow.treasurer.receipt') : t('cashflow.treasurer.payment'),
    },
    {
      key: 'amount',
      header: t('cashflow.treasurer.column.amount'),
      align: 'right',
      render: (row) => formatMoney(row.amount, locale),
    },
    {
      key: 'payer',
      header: t('cashflow.treasurer.column.payer'),
      render: (row) => row.payer_receiver_name ?? '',
    },
    {
      key: 'description',
      header: t('cashflow.treasurer.column.description'),
      render: (row) => row.description ?? '',
    },
  ]

  const listError =
    queue.error instanceof ApiError
      ? translateErrorCode(t, queue.error.errorCode)
      : queue.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t('cashflow.treasurer.title')}</h1>
          {!readOnly && items.length > 0 && (
            <Button
              variant="ghost"
              onClick={() => {
                setSelected(allSelected ? new Set() : new Set(items.map((item) => item.voucher_id)))
              }}
            >
              {allSelected
                ? t('cashflow.treasurer.clearAll')
                : t('cashflow.treasurer.pickAll')}
            </Button>
          )}
        </header>

        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {error !== null && <Alert tone="error">{error}</Alert>}
        {bookedMessage !== null && <Alert tone="info">{bookedMessage}</Alert>}

        <DataTable
          caption={t('cashflow.treasurer.title')}
          columns={columns}
          rows={items}
          rowKey={(row) => row.voucher_id}
          emptyLabel={t('cashflow.treasurer.empty')}
          loading={queue.isPending}
          loadingLabel={t('common.loading')}
          zebra
        />

        {!readOnly && (
          <footer className="flex flex-wrap items-end gap-3 rounded border border-border-default bg-background p-3">
            <Seg
              label={t('cashflow.treasurer.dateMode')}
              value={dateMode}
              onChange={(value) => {
                setDateMode(value === 'custom' ? 'custom' : 'posting_date')
              }}
              options={[
                { value: 'posting_date', label: t('cashflow.treasurer.byPostingDate') },
                { value: 'custom', label: t('cashflow.treasurer.byCustomDate') },
              ]}
            />
            {dateMode === 'custom' && (
              <TextField
                label={t('cashflow.treasurer.customDate')}
                type="date"
                value={customDate}
                max={todayIso()}
                onChange={(event) => {
                  setCustomDate(event.target.value)
                }}
              />
            )}
            <Button disabled={booker.isPending || selectedCount === 0} onClick={handleBook}>
              {booker.isPending
                ? t('cashflow.treasurer.booking')
                : t('cashflow.treasurer.book', { count: String(selectedCount) })}
            </Button>
          </footer>
        )}
      </section>
    </div>
  )
}
