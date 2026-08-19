/**
 * Màn hình danh sách chứng từ (U1) — tab "việc còn thiếu" lấy thẳng từ BFF
 * `pending-issues`, chọn một tab là lọc danh sách theo đúng loại chứng từ +
 * trạng thái "Đã cất" (còn việc phải ghi sổ) của tab đó.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { DataTableColumn, TabItem } from '@/design-system/components'
import { Alert, Button, DataTable, NextActionCell, StatusPill, Tabs } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { formatDate } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import { usePendingIssues } from './use-pending-issues'
import { useVoucherActions } from './use-voucher-actions'
import { VOUCHER_PAGE_SIZE, useVoucherList } from './use-voucher-list'
import type { VoucherListResponse } from './use-voucher-list'
import { voucherNextAction, voucherStatusLabel, voucherStatusTone } from './voucher-status'

const ALL_TAB = 'all'

type VoucherRow = VoucherListResponse['items'][number]

export function VoucherListPage(): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()

  const [activeTab, setActiveTab] = useState(ALL_TAB)
  const [page, setPage] = useState(1)
  const [actionError, setActionError] = useState<string | null>(null)

  const pendingIssues = usePendingIssues()
  const groups = pendingIssues.data?.unposted ?? []

  const list = useVoucherList(
    // `exactOptionalPropertyTypes`: bỏ hẳn khóa thay vì gán `undefined` khi
    // đứng ở tab "Tất cả" — hai khóa lọc chỉ có mặt lúc thật sự lọc.
    activeTab === ALL_TAB ? { page } : { documentType: activeTab, status: 1, page },
  )
  const actions = useVoucherActions()

  function fail(caught: unknown): void {
    setActionError(
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
    )
  }

  const tabs: TabItem[] = [
    { id: ALL_TAB, label: t('gl.voucher.tabAll') },
    ...groups.map((group) => ({ id: group.document_type, label: group.title, count: group.count })),
  ]

  const rows = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / VOUCHER_PAGE_SIZE))

  const columns: DataTableColumn<VoucherRow>[] = [
    {
      key: 'voucher_no',
      header: t('gl.voucher.column.no'),
      render: (row) => (
        <button
          type="button"
          className="text-secondary hover:underline"
          onClick={() => {
            void navigate(`/so-sach-thue/chung-tu/${row.id}`)
          }}
        >
          {row.voucher_no}
        </button>
      ),
    },
    {
      key: 'posting_date',
      header: t('gl.voucher.column.postingDate'),
      align: 'right',
      render: (row) => formatDate(row.posting_date, locale),
    },
    {
      key: 'description',
      header: t('gl.voucher.column.description'),
      render: (row) => row.description ?? '',
    },
    {
      key: 'status',
      header: t('gl.voucher.column.status'),
      render: (row) => (
        <StatusPill tone={voucherStatusTone(row.status)}>{voucherStatusLabel(t, row.status)}</StatusPill>
      ),
    },
    {
      key: 'nextAction',
      header: t('gl.voucher.column.nextAction'),
      render: (row) => (
        <NextActionCell
          action={voucherNextAction(t, row.status)}
          doneLabel={t('gl.voucher.nextAction.done')}
          {...(readOnly
            ? {}
            : {
                actionLabel: t('gl.voucher.nextAction.post'),
                onAction: () => {
                  setActionError(null)
                  actions.post.mutate(
                    { id: row.id, idempotencyKey: newIdempotencyKey() },
                    { onError: fail },
                  )
                },
              })}
        />
      ),
    },
  ]

  const listError =
    list.error instanceof ApiError
      ? translateErrorCode(t, list.error.errorCode)
      : list.isError
        ? t('error.transport.unreachable')
        : null

  const recalcPending = pendingIssues.data?.recalc_pending ?? []

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t('gl.voucher.title')}</h1>
          {!readOnly && (
            <Button
              onClick={() => {
                void navigate('/so-sach-thue/chung-tu/moi')
              }}
            >
              {t('gl.voucher.action.create')}
            </Button>
          )}
        </header>

        {recalcPending.length > 0 && <Alert tone="info">{t('gl.voucher.recalcPending')}</Alert>}
        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {actionError !== null && <Alert tone="error">{actionError}</Alert>}

        <Tabs
          label={t('gl.voucher.tabsLabel')}
          tabs={tabs}
          activeId={activeTab}
          onChange={(id) => {
            setActiveTab(id)
            setPage(1)
          }}
        >
          <div className="flex flex-col gap-2">
            <DataTable
              caption={t('gl.voucher.title')}
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              emptyLabel={t('gl.voucher.list.empty')}
              loading={list.isPending}
              loadingLabel={t('common.loading')}
              zebra
            />
            <footer className="flex items-center justify-between text-xs text-text-muted">
              <span>
                {t('gl.voucher.list.pageInfo', {
                  from: String(total === 0 ? 0 : (page - 1) * VOUCHER_PAGE_SIZE + 1),
                  to: String(Math.min(total, page * VOUCHER_PAGE_SIZE)),
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
                  {t('gl.voucher.list.prev')}
                </Button>
                <Button
                  variant="ghost"
                  disabled={page >= pageCount}
                  onClick={() => {
                    setPage((current) => current + 1)
                  }}
                >
                  {t('gl.voucher.list.next')}
                </Button>
              </span>
            </footer>
          </div>
        </Tabs>
      </section>
    </div>
  )
}
