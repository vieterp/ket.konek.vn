/**
 * Màn hình Số dư ban đầu (`/danh-muc-thiet-lap/so-du-ban-dau`, lát 4E,
 * FR-OPB-*).
 *
 * Một (năm tài chính, sổ) là phạm vi của cả màn hình — năm mới nhất tự chọn
 * lúc mở, cùng khuôn "điều chỉnh state trong thân render" của
 * `TrialBalancePage` (khuyến nghị React 18, không dùng `useEffect` cho việc
 * này). Năm mà server đã khóa (`is_closed`) hoặc phiên chỉ-đọc thì ẩn hết
 * hành động ghi — xem cột "chỉ đọc" của bảng hợp đồng ghi RT-09.
 *
 * Hai tổng dư Nợ/Có và cờ `balanced` tính trên **toàn bộ** (năm, sổ), không
 * theo tab đang đứng (FR-OPB-006) — server trả cùng hai số ở mọi tab, màn
 * hình chỉ đọc lại từ trang đang tải, không tự cộng (không phép tính tiền ở
 * client, docs/design-guidelines.md §5).
 *
 * Chuyển số dư từ năm trước và xóa số dư một nhóm là hai job chạy ngay trên
 * trang này (không qua drawer), nên việc "job vừa xong thì làm mới danh sách"
 * phải phản ứng với một tín hiệu tới **từ ngoài** vòng render (nhịp polling
 * của `useJob`) — đây là trường hợp hợp lệ để dùng `useEffect`, khác với
 * `ImportWizard`/`OpeningBalanceImportWizard` vốn chỉ cần làm mới khi người
 * dùng tự đóng (một sự kiện trong tay component).
 */

import type { ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import type { DataTableColumn, SelectOption, TabItem } from '@/design-system/components'
import { Alert, Button, DataTable, Seg, SelectField, StatusPill, Tabs } from '@/design-system/components'
import { formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import { OpeningBalanceImportWizard } from './opening-balance-import-wizard'
import {
  OPENING_BALANCE_PAGE_SIZE,
  carryForwardResultOf,
  clearGroupResultOf,
  invalidateOpeningBalances,
  isJobRunning,
  jobErrorCode,
  useCarryForwardOpeningBalances,
  useClearOpeningBalanceGroup,
  useDownloadOpeningBalanceTemplate,
  useFiscalYears,
  useJob,
  useOpeningBalances,
} from './use-opening-balances'
import type { OpeningBalanceRow } from './use-opening-balances'

const LEDGER_FINANCIAL = '0'
const LEDGER_MANAGEMENT = '1'

const DETAIL_KINDS = ['0', '1', '2', '3', '4'] as const
type DetailKind = (typeof DETAIL_KINDS)[number]

/** Nhóm 2/3/4 (Phải thu, Phải trả, Tạm ứng nhân viên) mới có đối tượng. */
const PARTNER_KINDS: readonly DetailKind[] = ['2', '3', '4']

const CARRY_FORWARD_EXISTS_CODE = 'opening.carry_forward_exists'

export function OpeningBalancePage(): ReactElement {
  const { t, locale } = useI18n()
  const { readOnly, datasetCode } = useSession()
  const queryClient = useQueryClient()

  const fiscalYears = useFiscalYears()

  const [fiscalYearId, setFiscalYearId] = useState<number | null>(null)
  const [ledger, setLedger] = useState(LEDGER_FINANCIAL)
  const [activeTab, setActiveTab] = useState<DetailKind>('0')
  const [page, setPage] = useState(1)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [carryForwardJobId, setCarryForwardJobId] = useState<string | null>(null)
  const [clearJobId, setClearJobId] = useState<string | null>(null)
  const [clearConfirm, setClearConfirm] = useState(false)

  // Năm mới nhất tự chọn khi dữ liệu vừa tải xong — chỉ MỘT lần (chặn bằng
  // `fiscalYearId !== null`), không ghi đè lựa chọn người dùng đã tự đổi.
  if (fiscalYearId === null && fiscalYears.data !== undefined && fiscalYears.data.length > 0) {
    const latestYear = fiscalYears.data[0]
    if (latestYear !== undefined) {
      setFiscalYearId(latestYear.id)
    }
  }

  const yearIndex = fiscalYears.data?.findIndex((year) => year.id === fiscalYearId) ?? -1
  const selectedYear = yearIndex >= 0 ? (fiscalYears.data?.[yearIndex] ?? null) : null
  // Danh sách xếp năm mới nhất trước (hợp đồng `FiscalYearListResponse`), nên
  // năm liền trước năm đang chọn nằm ở vị trí kế tiếp trong mảng.
  const prevYear = yearIndex >= 0 ? (fiscalYears.data?.[yearIndex + 1] ?? null) : null
  const yearClosed = selectedYear?.is_closed ?? false
  const canWrite = !readOnly && !yearClosed

  const listQuery = useOpeningBalances(
    fiscalYearId === null
      ? null
      : { fiscalYearId, ledger: Number(ledger), detailKind: Number(activeTab), page },
  )
  const download = useDownloadOpeningBalanceTemplate()
  const carryForward = useCarryForwardOpeningBalances()
  const clearGroup = useClearOpeningBalanceGroup()
  const carryForwardJob = useJob(carryForwardJobId)
  const clearJob = useJob(clearJobId)

  const carryForwardResult = carryForwardResultOf(carryForwardJob.data)
  const clearResult = clearGroupResultOf(clearJob.data)
  const carryForwardFailedCode =
    carryForwardJob.data?.status === 'failed' ? jobErrorCode(carryForwardJob.data.message) : null
  const carryForwardExists = carryForwardFailedCode === CARRY_FORWARD_EXISTS_CODE

  // Làm mới danh sách khi job (đang chạy trên chính trang này, không qua
  // drawer) chuyển sang "done" — phản ứng với nhịp polling bên ngoài luồng sự
  // kiện của người dùng, đúng việc `useEffect` sinh ra để làm.
  useEffect(() => {
    if (carryForwardJobId !== null && carryForwardJob.data?.status === 'done') {
      invalidateOpeningBalances(queryClient, datasetCode)
    }
  }, [carryForwardJobId, carryForwardJob.data?.status, queryClient, datasetCode])

  useEffect(() => {
    if (clearJobId !== null && clearJob.data?.status === 'done') {
      invalidateOpeningBalances(queryClient, datasetCode)
    }
  }, [clearJobId, clearJob.data?.status, queryClient, datasetCode])

  function fail(caught: unknown): void {
    setActionError(
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
    )
  }

  function runDownloadTemplate(): void {
    setActionError(null)
    download.mutate(undefined, { onError: fail })
  }

  function runCarryForward(overwrite: boolean): void {
    if (prevYear === null) {
      return
    }
    setActionError(null)
    carryForward.mutate(
      { fromFiscalYearId: prevYear.id, overwrite },
      { onSuccess: (response) => { setCarryForwardJobId(response.id) }, onError: fail },
    )
  }

  function runClearGroup(): void {
    if (!clearConfirm) {
      setClearConfirm(true)
      return
    }
    if (fiscalYearId === null) {
      return
    }
    setActionError(null)
    clearGroup.mutate(
      { fiscalYearId, ledger: Number(ledger), detailKind: Number(activeTab) },
      {
        onSuccess: (response) => {
          setClearJobId(response.id)
          setClearConfirm(false)
        },
        onError: fail,
      },
    )
  }

  const yearOptions: SelectOption[] = (fiscalYears.data ?? []).map((year) => ({
    value: String(year.id),
    label: year.code,
  }))

  const showPartnerColumn = PARTNER_KINDS.includes(activeTab)
  const columns: DataTableColumn<OpeningBalanceRow>[] = [
    { key: 'code', header: t('opening.column.account'), render: (row) => row.account_code },
    { key: 'name', header: t('opening.column.accountName'), render: (row) => row.account_name },
    ...(showPartnerColumn
      ? [
          {
            key: 'partner',
            header: t('opening.column.partner'),
            render: (row: OpeningBalanceRow) => row.partner_code ?? '',
          },
        ]
      : []),
    { key: 'currency', header: t('opening.column.currency'), render: (row) => row.currency_code },
    {
      key: 'debit',
      header: t('opening.column.debit'),
      align: 'right',
      render: (row) => formatMoney(row.debit, locale),
    },
    {
      key: 'credit',
      header: t('opening.column.credit'),
      align: 'right',
      render: (row) => formatMoney(row.credit, locale),
    },
  ]

  const rows = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / OPENING_BALANCE_PAGE_SIZE))
  const balanced = listQuery.data?.balanced ?? true

  const listError =
    listQuery.error instanceof ApiError
      ? translateErrorCode(t, listQuery.error.errorCode)
      : listQuery.isError
        ? t('error.transport.unreachable')
        : null

  const tabs: TabItem[] = DETAIL_KINDS.map((kind) => ({
    id: kind,
    label: t(`opening.detailKind.${kind}`),
  }))

  const carryForwardBusy = carryForward.isPending || isJobRunning(carryForwardJob.data)
  const clearBusy = clearGroup.isPending || isJobRunning(clearJob.data)

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t('opening.title')}</h1>
          <div className="flex flex-wrap items-end gap-3">
            <SelectField
              label={t('opening.fiscalYear')}
              value={fiscalYearId === null ? '' : String(fiscalYearId)}
              options={yearOptions}
              onChange={(event) => {
                setFiscalYearId(Number(event.target.value))
                setPage(1)
              }}
            />
            <Seg
              label={t('opening.ledger')}
              value={ledger}
              onChange={(value) => {
                setLedger(value)
                setPage(1)
              }}
              options={[
                { value: LEDGER_FINANCIAL, label: t('opening.ledger.financial') },
                { value: LEDGER_MANAGEMENT, label: t('opening.ledger.management') },
              ]}
            />
          </div>
        </header>

        <div className="flex flex-wrap items-center gap-3 text-sm text-text-default">
          <span>
            {t('opening.totalDebit')}: <strong>{formatMoney(listQuery.data?.total_debit ?? '0', locale)}</strong>
          </span>
          <span>
            {t('opening.totalCredit')}: <strong>{formatMoney(listQuery.data?.total_credit ?? '0', locale)}</strong>
          </span>
          <StatusPill tone={balanced ? 'ok' : 'bad'}>
            {balanced ? t('opening.balanced') : t('opening.unbalanced')}
          </StatusPill>
        </div>

        {!balanced && <Alert tone="warning">{t('opening.unbalancedWarning')}</Alert>}
        {yearClosed && <Alert tone="info">{t('opening.yearClosedNotice')}</Alert>}
        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {actionError !== null && <Alert tone="error">{actionError}</Alert>}

        <div className="flex flex-wrap items-center gap-2">
          {/* Tải tệp mẫu là hành động ĐỌC — hiện cả khi chỉ-đọc/năm đã khóa,
              cùng quy ước với màn danh mục (nút mẫu luôn có mặt). */}
          <Button variant="secondary" disabled={download.isPending} onClick={runDownloadTemplate}>
            {t('opening.action.downloadTemplate')}
          </Button>
          {canWrite && (
            <>
            <Button
              variant="secondary"
              onClick={() => {
                setWizardOpen(true)
              }}
            >
              {t('opening.action.import')}
            </Button>
            <Button
              variant="secondary"
              disabled={prevYear === null || carryForwardBusy}
              onClick={() => {
                runCarryForward(false)
              }}
            >
              {carryForwardBusy ? t('opening.carryForward.running') : t('opening.action.carryForward')}
            </Button>
            </>
          )}
        </div>

        {carryForwardJobId !== null && (
          <>
            {carryForwardJob.data?.status === 'failed' && carryForwardExists && (
              <div className="flex flex-wrap items-center gap-2">
                <Alert tone="warning">{translateErrorCode(t, carryForwardFailedCode ?? '')}</Alert>
                <Button
                  variant="secondary"
                  disabled={carryForwardBusy}
                  onClick={() => {
                    runCarryForward(true)
                  }}
                >
                  {t('opening.action.carryForwardOverwrite')}
                </Button>
              </div>
            )}
            {carryForwardJob.data?.status === 'failed' && !carryForwardExists && (
              <Alert tone="error">
                {t('opening.jobFailed', { message: carryForwardJob.data.message ?? '' })}
              </Alert>
            )}
            {carryForwardResult !== null && (
              <Alert tone="info">
                {t('opening.carryForward.summary', {
                  financial: String(carryForwardResult.rows_financial),
                  management: String(carryForwardResult.rows_management),
                })}
              </Alert>
            )}
          </>
        )}

        <Tabs
          label={t('opening.tabsLabel')}
          tabs={tabs}
          activeId={activeTab}
          onChange={(id) => {
            setActiveTab(id as DetailKind)
            setPage(1)
            setClearConfirm(false)
          }}
        >
          <div className="flex flex-col gap-2">
            <DataTable
              caption={t('opening.title')}
              columns={columns}
              rows={rows}
              rowKey={(row) => String(row.id)}
              emptyLabel={fiscalYearId === null ? t('opening.list.noYear') : t('opening.list.empty')}
              loading={listQuery.isPending && fiscalYearId !== null}
              loadingLabel={t('common.loading')}
              zebra
            />
            <footer className="flex items-center justify-between text-xs text-text-muted">
              <span>
                {t('opening.list.pageInfo', {
                  from: String(total === 0 ? 0 : (page - 1) * OPENING_BALANCE_PAGE_SIZE + 1),
                  to: String(Math.min(total, page * OPENING_BALANCE_PAGE_SIZE)),
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
                  {t('opening.list.prev')}
                </Button>
                <Button
                  variant="ghost"
                  disabled={page >= pageCount}
                  onClick={() => {
                    setPage((current) => current + 1)
                  }}
                >
                  {t('opening.list.next')}
                </Button>
              </span>
            </footer>

            {canWrite && (
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="ghost" disabled={clearBusy} onClick={runClearGroup}>
                  {clearConfirm ? t('opening.action.clearGroupConfirm') : t('opening.action.clearGroup')}
                </Button>
                {clearJob.data?.status === 'failed' && (
                  <Alert tone="error">
                    {t('opening.jobFailed', { message: clearJob.data.message ?? '' })}
                  </Alert>
                )}
                {clearResult !== null && (
                  <Alert tone="info">
                    {t('opening.clearGroup.done', { count: String(clearResult.deleted_rows) })}
                  </Alert>
                )}
              </div>
            )}
          </div>
        </Tabs>
      </section>

      {fiscalYearId !== null && (
        <OpeningBalanceImportWizard
          fiscalYearId={fiscalYearId}
          ledger={Number(ledger)}
          open={wizardOpen}
          onClose={() => {
            setWizardOpen(false)
          }}
        />
      )}
    </div>
  )
}
