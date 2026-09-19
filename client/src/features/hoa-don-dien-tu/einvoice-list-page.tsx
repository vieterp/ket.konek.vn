/**
 * Màn "Hóa đơn điện tử" (design nhóm 02 `#hddt-list`, U3): bốn thẻ đếm + năm
 * tab từ `counts_by_status`, lưới đọc `GET /einvoices` dạng hàng lưới với MỘT
 * cột trạng thái gộp "với cơ quan thuế" kèm việc cần làm, nút "Đồng bộ với
 * CQT" (bơm hàng đợi truyền tải 7E-1) và panel outbox gấp được.
 *
 * Bấm tab là đổi `tab=` gửi server — tab và thẻ đếm nói cùng một tập, không
 * lọc lại ở client. Bốn việc tại chỗ: phát hành / phát hành lại (hộp
 * `IssueEInvoiceDrawer`), gửi khách (drawer xem trước + ghi nhận gửi), sửa
 * sai sót (wizard U4). Đường `?source_voucher_id=` từ lưới bán 7H-2a mở
 * thẳng hộp phát hành cho chứng từ ấy.
 *
 * Hai tab đầu trang (Hóa đơn phát hành · Hóa đơn đầu vào) là hai ĐƯỜNG DẪN
 * (`/hoa-don-dien-tu` và `/dau-vao`, quyết định user 2026-09-18): dấu trang
 * và nút quay lại vẫn đúng.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'

import type { DataTableColumn, TabItem } from '@/design-system/components'
import {
  AdvancedSection,
  Alert,
  Button,
  DataTable,
  NextActionCell,
  StatusPill,
  Tabs,
  TextField,
} from '@/design-system/components'
import { formatDate, formatDateTime, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { EInvoicePreviewDrawer } from './einvoice-preview-drawer'
import {
  EINVOICE_STATUS_ADJUSTED,
  EINVOICE_STATUS_DRAFT,
  EINVOICE_STATUS_ISSUE_FAILED,
  EINVOICE_STATUS_ISSUED,
  EINVOICE_STATUS_ISSUING,
  EINVOICE_STATUS_REPLACED,
  EINVOICE_STATUS_SENT,
  canResolveError,
  einvoiceRowStatus,
  invoiceNumberText,
  isIssued,
} from './einvoice-row-status'
import { FeatureNav } from './feature-nav'
import { InboundTab } from './inbound-tab'
import { IssueEInvoiceDrawer } from './issue-einvoice-drawer'
import {
  EINVOICE_PAGE_SIZE,
  useEInvoiceActions,
  useEInvoiceList,
  useOutbox,
  type EInvoiceListItem,
  type EInvoiceListTab,
} from './use-einvoices'

const ALL_TAB = 'all'
const SOURCE_VOUCHER_PARAM = 'source_voucher_id'

export type EInvoicePageMode = 'outbound' | 'inbound'

function countOf(counts: Record<string, number> | undefined, ...statuses: number[]): number | undefined {
  if (counts === undefined) {
    return undefined
  }
  return statuses.reduce((sum, status) => sum + (counts[String(status)] ?? 0), 0)
}

function StatCard({
  label,
  value,
  hint,
  tone,
}: {
  readonly label: string
  readonly value: number | undefined
  readonly hint: string
  readonly tone: 'todo' | 'bad' | 'ok'
}): ReactElement {
  const border =
    tone === 'bad' ? 'border-status-bad' : tone === 'todo' ? 'border-primary' : 'border-border-default'
  return (
    <div className={`flex-1 rounded border p-3 ${border}`}>
      <div className="text-xs text-text-muted">{label}</div>
      <div className="text-2xl font-semibold tabular-nums text-primary">
        {value === undefined ? '…' : value}
      </div>
      <div className="text-xs text-text-muted">{hint}</div>
    </div>
  )
}

/** Panel vận hành hàng đợi truyền tải — chỉ đọc; dòng `needs_reconcile` là việc của worker (7E-1). */
function OutboxPanel(): ReactElement {
  const { t, locale } = useI18n()
  const outbox = useOutbox()
  const rows = outbox.data?.items ?? []
  return (
    <AdvancedSection
      label={t('einvoice.outbox.title', { due: String(outbox.data?.due_now ?? 0) })}
    >
      {rows.length === 0 ? (
        <p className="text-sm text-text-muted">{t('einvoice.outbox.empty')}</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm">
          {rows.map((row) => (
            <li key={row.id} className="flex flex-wrap items-baseline gap-2">
              <span className="font-mono text-xs text-text-muted">#{row.id}</span>
              <span>{t(`einvoice.outbox.operation.${row.operation}` as 'einvoice.outbox.operation.issue')}</span>
              <StatusPill tone={row.status === 'done' ? 'ok' : row.status === 'in_flight' ? 'todo' : 'bad'}>
                {t(`einvoice.outbox.status.${row.status}` as 'einvoice.outbox.status.done')}
              </StatusPill>
              <span className="text-xs text-text-muted">
                {t('einvoice.outbox.attempts', { count: String(row.attempt_count) })} ·{' '}
                {formatDateTime(row.created_at, locale)}
              </span>
              {row.last_error !== null && (
                <span className="text-xs text-status-bad">{row.last_error}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </AdvancedSection>
  )
}

function OutboundList(): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()
  const [searchParams, setSearchParams] = useSearchParams()
  const [activeTab, setActiveTab] = useState(ALL_TAB)
  const [page, setPage] = useState(1)
  const [searchText, setSearchText] = useState('')
  const [q, setQ] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [issuing, setIssuing] = useState<string | null>(
    () => searchParams.get(SOURCE_VOUCHER_PARAM),
  )
  const [previewing, setPreviewing] = useState<EInvoiceListItem | null>(null)

  const list = useEInvoiceList({
    page,
    q,
    ...(activeTab === ALL_TAB ? {} : { tab: activeTab as EInvoiceListTab }),
  })
  const actions = useEInvoiceActions()

  function fail(caught: unknown): void {
    setActionError(
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
    )
  }

  const counts = list.data?.counts_by_status
  const tabs: TabItem[] = [
    { id: ALL_TAB, label: t('einvoice.list.tabAll') },
    { id: 'cho-phat-hanh', label: t('einvoice.list.tab.cho-phat-hanh'), ...countProp(countOf(counts, EINVOICE_STATUS_DRAFT)) },
    { id: 'can-xu-ly', label: t('einvoice.list.tab.can-xu-ly'), ...countProp(countOf(counts, EINVOICE_STATUS_ISSUE_FAILED)) },
    { id: 'da-thay-the-dieu-chinh', label: t('einvoice.list.tab.da-thay-the-dieu-chinh'), ...countProp(countOf(counts, EINVOICE_STATUS_REPLACED, EINVOICE_STATUS_ADJUSTED)) },
    { id: 'khach-chua-nhan', label: t('einvoice.list.tab.khach-chua-nhan'), ...countProp(countOf(counts, EINVOICE_STATUS_ISSUED)) },
  ]

  const rows = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / EINVOICE_PAGE_SIZE))

  function closeIssue(): void {
    setIssuing(null)
    if (searchParams.has(SOURCE_VOUCHER_PARAM)) {
      const next = new URLSearchParams(searchParams)
      next.delete(SOURCE_VOUCHER_PARAM)
      setSearchParams(next, { replace: true })
    }
  }

  function runNextAction(row: EInvoiceListItem): void {
    const status = einvoiceRowStatus(t, locale, row)
    setActionError(null)
    if (status.nextAction === 'issue' || status.nextAction === 'reissue') {
      setIssuing(row.source_voucher_id)
    } else if (status.nextAction === 'send') {
      setPreviewing(row)
    }
  }

  const columns: DataTableColumn<EInvoiceListItem>[] = [
    {
      key: 'invoice_date',
      header: t('einvoice.list.column.date'),
      render: (row) => (row.invoice_date === null ? '—' : formatDate(row.invoice_date, locale)),
    },
    {
      key: 'invoice_no',
      header: t('einvoice.list.column.no'),
      render: (row) =>
        isIssued(row) ? (
          <button
            type="button"
            className="text-secondary hover:underline"
            onClick={() => {
              setPreviewing(row)
            }}
          >
            {invoiceNumberText(t, row)}
          </button>
        ) : (
          <span className="text-text-muted">{invoiceNumberText(t, row)}</span>
        ),
    },
    {
      key: 'customer',
      header: t('einvoice.list.column.customer'),
      render: (row) => row.customer_name ?? row.customer_code ?? '',
    },
    {
      key: 'voucher_no',
      header: t('einvoice.list.column.voucher'),
      render: (row) => (
        <button
          type="button"
          className="text-secondary hover:underline"
          onClick={() => {
            void navigate(`/ban-hang/chung-tu/${row.source_voucher_id}`)
          }}
        >
          {row.voucher_no}
        </button>
      ),
    },
    {
      key: 'total_fc',
      header: t('einvoice.list.column.total'),
      align: 'right',
      render: (row) => formatMoney(row.total_fc, locale),
    },
    {
      key: 'status',
      header: t('einvoice.list.column.status'),
      render: (row) => {
        const status = einvoiceRowStatus(t, locale, row)
        return <StatusPill tone={status.tone}>{status.label}</StatusPill>
      },
    },
    {
      key: 'delivery',
      header: t('einvoice.list.column.delivery'),
      render: (row) => {
        const status = einvoiceRowStatus(t, locale, row)
        return status.delivery === null ? (
          '—'
        ) : (
          <StatusPill tone={status.delivery.tone}>{status.delivery.label}</StatusPill>
        )
      },
    },
    {
      key: 'nextAction',
      header: t('einvoice.list.column.nextAction'),
      render: (row) => {
        const status = einvoiceRowStatus(t, locale, row)
        return (
          <NextActionCell
            action={status.actionLabel}
            doneLabel={t('einvoice.list.action.done')}
            {...(readOnly || status.nextAction === null
              ? {}
              : {
                  actionLabel: status.actionLabel ?? '',
                  onAction: () => {
                    runNextAction(row)
                  },
                })}
          />
        )
      },
    },
    {
      key: 'more',
      header: t('einvoice.list.column.more'),
      render: (row) => (
        <span className="flex gap-2 text-xs">
          {!readOnly && canResolveError(row) && (
            <button
              type="button"
              className="text-secondary hover:underline"
              onClick={() => {
                void navigate(`/hoa-don-dien-tu/${row.id}/xu-ly`)
              }}
            >
              {t('einvoice.list.action.resolveError')}
            </button>
          )}
          {!readOnly && row.status === EINVOICE_STATUS_DRAFT && (
            <button
              type="button"
              className="text-status-bad hover:underline"
              onClick={() => {
                setActionError(null)
                actions.remove.mutate(row.id, { onError: fail })
              }}
            >
              {t('einvoice.list.action.delete')}
            </button>
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
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-3">
        <StatCard
          label={t('einvoice.stat.draft')}
          value={countOf(counts, EINVOICE_STATUS_DRAFT)}
          hint={t('einvoice.stat.draftHint')}
          tone="todo"
        />
        <StatCard
          label={t('einvoice.stat.issuing')}
          value={countOf(counts, EINVOICE_STATUS_ISSUING)}
          hint={t('einvoice.stat.issuingHint')}
          tone="todo"
        />
        <StatCard
          label={t('einvoice.stat.rejected')}
          value={countOf(counts, EINVOICE_STATUS_ISSUE_FAILED)}
          hint={t('einvoice.stat.rejectedHint')}
          tone="bad"
        />
        <StatCard
          label={t('einvoice.stat.issued')}
          value={countOf(counts, EINVOICE_STATUS_ISSUED, EINVOICE_STATUS_SENT)}
          hint={t('einvoice.stat.issuedHint')}
          tone="ok"
        />
      </div>

      {listError !== null && <Alert tone="error">{listError}</Alert>}
      {actionError !== null && <Alert tone="error">{actionError}</Alert>}
      {notice !== null && <Alert tone="info">{notice}</Alert>}

      <Tabs
        label={t('einvoice.list.tabsLabel')}
        tabs={tabs}
        activeId={activeTab}
        onChange={(id) => {
          setActiveTab(id)
          setPage(1)
        }}
      >
        <div className="flex flex-col gap-2">
          <form
            className="flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              setQ(searchText)
              setPage(1)
            }}
          >
            <TextField
              label={t('einvoice.list.search')}
              placeholder={t('einvoice.list.searchPlaceholder')}
              value={searchText}
              onChange={(event) => {
                setSearchText(event.target.value)
              }}
            />
            <Button type="submit" variant="secondary">
              {t('einvoice.list.searchSubmit')}
            </Button>
          </form>
          <DataTable
            caption={t('einvoice.list.title')}
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            emptyLabel={t('einvoice.list.empty')}
            loading={list.isPending}
            loadingLabel={t('common.loading')}
            zebra
          />
          <footer className="flex items-center justify-between text-xs text-text-muted">
            <span>
              {t('einvoice.list.pageInfo', {
                from: String(total === 0 ? 0 : (page - 1) * EINVOICE_PAGE_SIZE + 1),
                to: String(Math.min(total, page * EINVOICE_PAGE_SIZE)),
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
                {t('einvoice.list.prev')}
              </Button>
              <Button
                variant="ghost"
                disabled={page >= pageCount}
                onClick={() => {
                  setPage((current) => current + 1)
                }}
              >
                {t('einvoice.list.next')}
              </Button>
            </span>
          </footer>
        </div>
      </Tabs>

      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <OutboxPanel />
        </div>
        {!readOnly && (
          <Button
            variant="secondary"
            disabled={actions.pump.isPending}
            onClick={() => {
              setActionError(null)
              actions.pump.mutate(undefined, {
                onSuccess: () => {
                  setNotice(t('einvoice.list.pumpQueued'))
                },
                onError: fail,
              })
            }}
          >
            {t('einvoice.list.action.pump')}
          </Button>
        )}
      </div>

      {issuing !== null && (
        <IssueEInvoiceDrawer
          open
          sourceVoucherId={issuing}
          onClose={closeIssue}
          onIssued={() => {
            setNotice(t('einvoice.list.issued'))
            closeIssue()
          }}
        />
      )}
      <EInvoicePreviewDrawer
        key={previewing?.id ?? 'none'}
        invoice={previewing}
        onClose={() => {
          setPreviewing(null)
        }}
      />
    </div>
  )
}

function countProp(count: number | undefined): { readonly count?: number } {
  return count === undefined ? {} : { count }
}

export function EInvoiceListPage({ mode }: { readonly mode: EInvoicePageMode }): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const modeTabs: TabItem[] = [
    { id: 'outbound', label: t('einvoice.mode.outbound') },
    { id: 'inbound', label: t('einvoice.mode.inbound') },
  ]
  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header>
          <h1 className="text-lg font-semibold text-primary">{t('einvoice.list.title')}</h1>
        </header>
        <Tabs
          label={t('einvoice.mode.label')}
          tabs={modeTabs}
          activeId={mode}
          onChange={(id) => {
            void navigate(id === 'inbound' ? '/hoa-don-dien-tu/dau-vao' : '/hoa-don-dien-tu')
          }}
        >
          {mode === 'inbound' ? <InboundTab /> : <OutboundList />}
        </Tabs>
      </section>
    </div>
  )
}
