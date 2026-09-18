/**
 * Màn "Chứng từ bán hàng" (bộ xương màn 01 `#mua-list` dùng lại, U1): tab nói
 * thẳng việc còn thiếu (BFF `pending-issues` 7G-4, chiều bán) + lưới chứng từ
 * đọc `GET /sales/invoices` với cột khách hàng, hóa đơn, tổng tiền, còn phải
 * thu, trạng thái ba tông và "Việc tiếp theo" làm được tại chỗ.
 *
 * Bấm một tab là đổi bộ lọc lưới sang cùng định nghĩa của nhóm ấy ở server,
 * không lọc lại ở client. Tab đếm KHOẢN còn việc, lưới liệt kê CHỨNG TỪ — hai
 * con số có thể lệch có chủ đích (7H-1 M-1): nợ mang sang từ số dư ban đầu
 * không có chứng từ để mở. Ba việc: ghi sổ tại chỗ (`useVoucherActions`), phát
 * hành hóa đơn (dẫn sang nhóm 02 kèm `source_voucher_id` — wizard 7H-3 nhận),
 * lập phiếu thu (form phiếu thu của nhóm 03, điền sẵn khách — phiếu thu KHÔNG
 * lập từ form bán, `SalesInvoiceIn` không có đường thu ngay).
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { DataTableColumn, TabItem } from '@/design-system/components'
import {
  Alert,
  Button,
  DataTable,
  NextActionCell,
  SelectField,
  StatusPill,
  Tabs,
} from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useVoucherActions } from '@/features/so-sach-thue/use-voucher-actions'

import { FeatureNav } from './feature-nav'
import {
  SALES_CREATE_KINDS,
  SALES_KIND_GOODS,
  SALES_KIND_LABEL_KEYS,
  invoiceText,
  salesRowStatus,
} from './sales-row-status'
import {
  SALES_PAGE_SIZE,
  useSalesInvoiceList,
  useSalesPendingIssues,
  type SalesInvoiceListItem,
  type SalesInvoiceListQuery,
} from './use-sales-invoices'

const ALL_TAB = 'all'

/** Mã nhóm của BFF → bộ lọc lưới. Mã lạ (nhóm phase 8 sau này) rơi về "Tất cả". */
function queryForTab(tab: string, page: number): SalesInvoiceListQuery {
  switch (tab) {
    case 'chua-ghi-so':
      return { page, status: 1 }
    case 'chua-co-hoa-don':
      return { page, status: 2, einvoice: 'missing' }
    case 'qua-han':
      return { page, overdue: true }
    default:
      return { page }
  }
}

export function SalesListPage(): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()
  const [activeTab, setActiveTab] = useState(ALL_TAB)
  const [page, setPage] = useState(1)
  const [createKind, setCreateKind] = useState(String(SALES_KIND_GOODS))
  const [actionError, setActionError] = useState<string | null>(null)

  const pending = useSalesPendingIssues()
  const list = useSalesInvoiceList(queryForTab(activeTab, page))
  const actions = useVoucherActions()

  function fail(caught: unknown): void {
    setActionError(
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
    )
  }

  const groups = pending.data?.groups ?? []
  const tabs: TabItem[] = [
    { id: ALL_TAB, label: t('sales.list.tabAll') },
    ...groups.map((group) => ({
      id: group.code,
      label: t(`sales.list.tab.${group.code}` as 'sales.list.tab.chua-ghi-so'),
      count: group.count,
    })),
  ]

  const rows = list.data?.items ?? []
  // Một dòng tổng MỖI tiền tệ (server nhóm) — bộ sổ chỉ VND ra đúng một dòng.
  const totals = list.data?.totals ?? []
  const total = list.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / SALES_PAGE_SIZE))

  function openInvoice(row: SalesInvoiceListItem): void {
    void navigate(`/ban-hang/chung-tu/${row.id}`)
  }

  function runNextAction(row: SalesInvoiceListItem): void {
    const status = salesRowStatus(t, row)
    if (status.nextAction === 'post') {
      setActionError(null)
      actions.post.mutate({ id: row.id, idempotencyKey: newIdempotencyKey() }, { onError: fail })
    } else if (status.nextAction === 'issue-einvoice') {
      void navigate(`/hoa-don-dien-tu?source_voucher_id=${row.id}`)
    } else if (status.nextAction === 'collect') {
      void navigate(
        `/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0&partner_id=${String(row.customer_id)}`,
      )
    }
  }

  const columns: DataTableColumn<SalesInvoiceListItem>[] = [
    {
      key: 'posting_date',
      header: t('sales.list.column.date'),
      render: (row) => formatDate(row.posting_date, locale),
    },
    {
      key: 'voucher_no',
      header: t('sales.list.column.no'),
      render: (row) => (
        <button
          type="button"
          className="text-secondary hover:underline"
          onClick={() => {
            openInvoice(row)
          }}
        >
          {row.voucher_no}
        </button>
      ),
    },
    {
      key: 'customer',
      header: t('sales.list.column.customer'),
      render: (row) => row.customer_name ?? row.customer_code ?? '',
    },
    {
      key: 'invoice',
      header: t('sales.list.column.invoice'),
      render: (row) => {
        const invoice = invoiceText(t, row)
        return invoice.missing ? (
          <span className="text-status-bad">{invoice.text}</span>
        ) : (
          <span>{invoice.text}</span>
        )
      },
    },
    {
      key: 'total_fc',
      header: t('sales.list.column.total'),
      align: 'right',
      render: (row) => formatMoney(row.total_fc, locale),
    },
    {
      key: 'remaining_fc',
      header: t('sales.list.column.remaining'),
      align: 'right',
      // Chứng từ chưa ghi sổ / giảm trừ không có khoản nợ riêng → dấu gạch,
      // không phải số 0 (0 là "đã thu hết", một câu khác hẳn).
      render: (row) => (row.remaining_fc === null ? '—' : formatMoney(row.remaining_fc, locale)),
    },
    {
      key: 'status',
      header: t('sales.list.column.status'),
      render: (row) => {
        const status = salesRowStatus(t, row)
        return <StatusPill tone={status.tone}>{status.label}</StatusPill>
      },
    },
    {
      key: 'nextAction',
      header: t('sales.list.column.nextAction'),
      render: (row) => {
        const status = salesRowStatus(t, row)
        return (
          <NextActionCell
            action={status.actionLabel}
            doneLabel={t('sales.list.action.done')}
            {...(readOnly || status.actionLabel === null
              ? {}
              : {
                  actionLabel: status.actionLabel,
                  onAction: () => {
                    runNextAction(row)
                  },
                })}
          />
        )
      },
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
          <div>
            <h1 className="text-lg font-semibold text-primary">{t('sales.list.title')}</h1>
            {list.data !== undefined && (
              <p className="text-xs text-text-muted">
                {t('sales.list.subtitle', { count: String(total) })}
              </p>
            )}
          </div>
          {!readOnly && (
            <div className="flex items-end gap-2">
              <SelectField
                label={t('sales.list.createKind')}
                labelHidden
                value={createKind}
                onChange={(event) => {
                  setCreateKind(event.target.value)
                }}
                options={SALES_CREATE_KINDS.map((kind) => ({
                  value: String(kind),
                  label: t(SALES_KIND_LABEL_KEYS[kind as keyof typeof SALES_KIND_LABEL_KEYS]),
                }))}
              />
              <Button
                onClick={() => {
                  void navigate(`/ban-hang/chung-tu/moi?kind=${createKind}`)
                }}
              >
                {t('sales.list.action.create')}
              </Button>
            </div>
          )}
        </header>

        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {actionError !== null && <Alert tone="error">{actionError}</Alert>}

        <Tabs
          label={t('sales.list.tabsLabel')}
          tabs={tabs}
          activeId={activeTab}
          onChange={(id) => {
            setActiveTab(id)
            setPage(1)
          }}
        >
          <div className="flex flex-col gap-2">
            <DataTable
              caption={t('sales.list.title')}
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              emptyLabel={t('sales.list.empty')}
              loading={list.isPending}
              loadingLabel={t('common.loading')}
              zebra
              {...(totals.length === 0
                ? {}
                : {
                    totals: totals.map((row) => ({
                      key: `sum-${row.currency_code}`,
                      label:
                        totals.length === 1
                          ? t('sales.list.totalsLabel', { count: String(row.count) })
                          : t('sales.list.totalsLabelCurrency', {
                              count: String(row.count),
                              currency: row.currency_code,
                            }),
                      cells: {
                        total_fc: formatMoney(row.total_fc, locale),
                        remaining_fc: (
                          <span className="text-status-bad">{formatMoney(row.remaining_fc, locale)}</span>
                        ),
                      },
                    })),
                  })}
            />
            <footer className="flex items-center justify-between text-xs text-text-muted">
              <span>
                {t('sales.list.pageInfo', {
                  from: String(total === 0 ? 0 : (page - 1) * SALES_PAGE_SIZE + 1),
                  to: String(Math.min(total, page * SALES_PAGE_SIZE)),
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
                  {t('sales.list.prev')}
                </Button>
                <Button
                  variant="ghost"
                  disabled={page >= pageCount}
                  onClick={() => {
                    setPage((current) => current + 1)
                  }}
                >
                  {t('sales.list.next')}
                </Button>
              </span>
            </footer>
          </div>
        </Tabs>
      </section>
    </div>
  )
}
