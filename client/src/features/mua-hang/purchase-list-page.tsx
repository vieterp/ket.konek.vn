/**
 * Màn "Chứng từ mua hàng" (design màn 01 `#mua-list`, U1): tab nói thẳng việc
 * còn thiếu (BFF `pending-issues` 7G-4) + lưới chứng từ đọc `GET
 * /purchase/invoices` với cột NCC, hóa đơn NCC, tổng tiền, còn phải trả, trạng
 * thái ba tông và "Việc tiếp theo" làm được tại chỗ.
 *
 * Bấm một tab là đổi bộ lọc lưới sang cùng định nghĩa của nhóm ấy ở server,
 * không lọc lại ở client. Tab đếm KHOẢN còn việc, lưới liệt kê CHỨNG TỪ — hai
 * con số có thể lệch có chủ đích (user chốt 2026-09-18): nợ mang sang từ số dư
 * ban đầu không có chứng từ để mở, và khoản chi phí mua do NCC khác thu là một
 * khoản riêng trên cùng chứng từ; lưới chỉ nói về công nợ với NCC ghi trên hóa
 * đơn, phần kia sống ở báo cáo tuổi nợ / thẻ đối tác. Ba việc: ghi sổ tại chỗ
 * (`useVoucherActions`), bổ sung hóa đơn (mở form), lập phiếu chi (form phiếu
 * chi của nhóm 03, điền sẵn NCC — phiếu chi KHÔNG lập từ form mua,
 * `PurchaseInvoiceIn` không có đường trả ngay).
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
  PURCHASE_KIND_GOODS,
  PURCHASE_KIND_LABEL_KEYS,
  VENDOR_INVOICE_NOT_YET,
  purchaseRowStatus,
  vendorInvoiceText,
} from './purchase-row-status'
import {
  PURCHASE_PAGE_SIZE,
  usePurchaseInvoiceList,
  usePurchasePendingIssues,
  type PurchaseInvoiceListItem,
  type PurchaseInvoiceListQuery,
} from './use-purchase-invoices'

const ALL_TAB = 'all'

/** Mã nhóm của BFF → bộ lọc lưới. Mã lạ (nhóm phase 8 sau này) rơi về "Tất cả". */
function queryForTab(tab: string, page: number): PurchaseInvoiceListQuery {
  switch (tab) {
    case 'chua-ghi-so':
      return { page, status: 1 }
    case 'chua-co-hoa-don':
      return { page, status: 2, vendorInvoiceStatus: VENDOR_INVOICE_NOT_YET }
    case 'qua-han':
      return { page, overdue: true }
    default:
      return { page }
  }
}

export function PurchaseListPage(): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()
  const [activeTab, setActiveTab] = useState(ALL_TAB)
  const [page, setPage] = useState(1)
  const [createKind, setCreateKind] = useState(String(PURCHASE_KIND_GOODS))
  const [actionError, setActionError] = useState<string | null>(null)

  const pending = usePurchasePendingIssues()
  const list = usePurchaseInvoiceList(queryForTab(activeTab, page))
  const actions = useVoucherActions()

  function fail(caught: unknown): void {
    setActionError(
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
    )
  }

  const groups = pending.data?.groups ?? []
  const tabs: TabItem[] = [
    { id: ALL_TAB, label: t('purchase.list.tabAll') },
    ...groups.map((group) => ({
      id: group.code,
      label: t(`purchase.list.tab.${group.code}` as 'purchase.list.tab.chua-ghi-so'),
      count: group.count,
    })),
  ]

  const rows = list.data?.items ?? []
  // Một dòng tổng MỖI tiền tệ (server nhóm) — bộ sổ chỉ VND ra đúng một dòng.
  const totals = list.data?.totals ?? []
  const total = list.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PURCHASE_PAGE_SIZE))

  function openInvoice(row: PurchaseInvoiceListItem): void {
    void navigate(`/mua-hang/chung-tu/${row.id}`)
  }

  function runNextAction(row: PurchaseInvoiceListItem): void {
    const status = purchaseRowStatus(t, row)
    if (status.nextAction === 'post') {
      setActionError(null)
      actions.post.mutate({ id: row.id, idempotencyKey: newIdempotencyKey() }, { onError: fail })
    } else if (status.nextAction === 'add-invoice') {
      openInvoice(row)
    } else if (status.nextAction === 'pay') {
      void navigate(
        `/tien-vao-tien-ra/giao-dich/phieu/moi?kind=1&partner_id=${String(row.vendor_id)}`,
      )
    }
  }

  const columns: DataTableColumn<PurchaseInvoiceListItem>[] = [
    {
      key: 'posting_date',
      header: t('purchase.list.column.date'),
      render: (row) => formatDate(row.posting_date, locale),
    },
    {
      key: 'voucher_no',
      header: t('purchase.list.column.no'),
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
      key: 'vendor',
      header: t('purchase.list.column.vendor'),
      render: (row) => row.vendor_name ?? row.vendor_code ?? '',
    },
    {
      key: 'vendor_invoice',
      header: t('purchase.list.column.vendorInvoice'),
      render: (row) => {
        const invoice = vendorInvoiceText(t, row)
        return invoice.missing ? (
          <span className="text-status-bad">{invoice.text}</span>
        ) : (
          <span>{invoice.text}</span>
        )
      },
    },
    {
      key: 'total_fc',
      header: t('purchase.list.column.total'),
      align: 'right',
      render: (row) => formatMoney(row.total_fc, locale),
    },
    {
      key: 'remaining_fc',
      header: t('purchase.list.column.remaining'),
      align: 'right',
      // Chứng từ chưa ghi sổ / trả lại hàng không có khoản nợ riêng → dấu gạch,
      // không phải số 0 (0 là "đã trả hết", một câu khác hẳn).
      render: (row) => (row.remaining_fc === null ? '—' : formatMoney(row.remaining_fc, locale)),
    },
    {
      key: 'status',
      header: t('purchase.list.column.status'),
      render: (row) => {
        const status = purchaseRowStatus(t, row)
        return <StatusPill tone={status.tone}>{status.label}</StatusPill>
      },
    },
    {
      key: 'nextAction',
      header: t('purchase.list.column.nextAction'),
      render: (row) => {
        const status = purchaseRowStatus(t, row)
        return (
          <NextActionCell
            action={status.actionLabel}
            doneLabel={t('purchase.list.action.done')}
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
            <h1 className="text-lg font-semibold text-primary">{t('purchase.list.title')}</h1>
            {list.data !== undefined && (
              <p className="text-xs text-text-muted">
                {t('purchase.list.subtitle', { count: String(total) })}
              </p>
            )}
          </div>
          {!readOnly && (
            <div className="flex items-end gap-2">
              <SelectField
                label={t('purchase.list.createKind')}
                labelHidden
                value={createKind}
                onChange={(event) => {
                  setCreateKind(event.target.value)
                }}
                options={Object.entries(PURCHASE_KIND_LABEL_KEYS).map(([value, key]) => ({
                  value,
                  label: t(key),
                }))}
              />
              <Button
                onClick={() => {
                  void navigate(`/mua-hang/chung-tu/moi?kind=${createKind}`)
                }}
              >
                {t('purchase.list.action.create')}
              </Button>
            </div>
          )}
        </header>

        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {actionError !== null && <Alert tone="error">{actionError}</Alert>}

        <Tabs
          label={t('purchase.list.tabsLabel')}
          tabs={tabs}
          activeId={activeTab}
          onChange={(id) => {
            setActiveTab(id)
            setPage(1)
          }}
        >
          <div className="flex flex-col gap-2">
            <DataTable
              caption={t('purchase.list.title')}
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              emptyLabel={t('purchase.list.empty')}
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
                          ? t('purchase.list.totalsLabel', { count: String(row.count) })
                          : t('purchase.list.totalsLabelCurrency', {
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
                {t('purchase.list.pageInfo', {
                  from: String(total === 0 ? 0 : (page - 1) * PURCHASE_PAGE_SIZE + 1),
                  to: String(Math.min(total, page * PURCHASE_PAGE_SIZE)),
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
                  {t('purchase.list.prev')}
                </Button>
                <Button
                  variant="ghost"
                  disabled={page >= pageCount}
                  onClick={() => {
                    setPage((current) => current + 1)
                  }}
                >
                  {t('purchase.list.next')}
                </Button>
              </span>
            </footer>
          </div>
        </Tabs>
      </section>
    </div>
  )
}
