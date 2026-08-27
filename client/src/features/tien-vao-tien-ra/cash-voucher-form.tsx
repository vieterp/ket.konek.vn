/**
 * Form phiếu thu/chi tiền mặt (bước 19, U2) — `/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0|1`
 * và `.../phieu/:id`.
 *
 * Chọn nghiệp vụ (FR-SYS-025) → điền sẵn cặp Nợ/Có vào dòng đầu (vẫn sửa
 * được); chọn đối tác → khối đối trừ công nợ liệt kê chứng từ còn nợ
 * (`docs/srs/03` §4). Cảnh báo FR-SYS-062 (ví dụ chi quá tồn quỹ) quay về dạng
 * 422 toàn-cảnh-báo → băng "Vẫn ghi sổ?" gửi lại kèm `acknowledge_warnings`.
 *
 * Cùng khung xương `so-sach-thue/journal-voucher-form.tsx`: định tuyến theo
 * `:id`, form dựng từ dữ liệu ĐÃ tải (`key={voucher.id}`), lưới hydrate đúng
 * một lần trong thân render khi các lượt tra danh mục xong.
 */

import type { ReactElement, ReactNode } from 'react'
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'

import type { DataGridChange, LookupOption } from '@/design-system/components'
import { Alert, DataGrid } from '@/design-system/components'
import { useAccess } from '@/lib/access'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { DIMENSION_COLUMNS } from '@/features/so-sach-thue/dimension-config'
import { JournalViolationsAlert } from '@/features/so-sach-thue/journal-violations-alert'
import { extractViolations, type Violation } from '@/features/so-sach-thue/journal-violations'
import { JournalVoucherActionsFooter } from '@/features/so-sach-thue/journal-voucher-actions-footer'
import { todayIso } from '@/features/so-sach-thue/local-date'
import { useAccountLookup } from '@/features/so-sach-thue/use-account-lookup'
import { useDimensionLookups } from '@/features/so-sach-thue/use-dimension-lookups'
import { useVoucherActions } from '@/features/so-sach-thue/use-voucher-actions'

import { CashVoucherHeaderFields } from './cash-voucher-header-fields'
import { FeatureNav } from './feature-nav'
import { buildPairLineColumns } from './pair-line-columns'
import { buildRowFromPairLine } from './pair-line-hydrate'
import { resolvePairLines } from './pair-line-resolve'
import { applyPairLineChanges, emptyPairLineRow, type PairLineRow } from './pair-line-types'
import { SettlementSection, settlementKey } from './settlement-section'
import { useAutoPostingOperations } from './use-auto-posting'
import {
  useCashVoucher,
  useCreateCashVoucher,
  useUpdateCashVoucher,
  type CashVoucherIn,
  type CashVoucherOut,
  type CashVoucherUpdate,
} from './use-cash-voucher'
import { useMasterSearchLookup } from './use-master-search-lookup'

/** TK quỹ tiền mặt nhận diện theo tiền tố số hiệu — cùng luật server (`CASH_ON_HAND_PREFIX`). */
const CASH_ACCOUNT_PREFIX = '111'

const KIND_RECEIPT = 0

function FormShell({
  title,
  children,
}: {
  readonly title: string
  readonly children: ReactNode
}): ReactElement {
  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <h1 className="text-lg font-semibold text-primary">{title}</h1>
        {children}
      </section>
    </div>
  )
}

export function CashVoucherForm(): ReactElement {
  const { id } = useParams<{ id?: string }>()
  return id === undefined ? <NewVoucherPage /> : <ExistingVoucherPage id={id} />
}

function NewVoucherPage(): ReactElement {
  const { t } = useI18n()
  const [searchParams] = useSearchParams()
  const kind = searchParams.get('kind') === '1' ? 1 : KIND_RECEIPT
  const title =
    kind === KIND_RECEIPT ? t('cashflow.form.titleCreateReceipt') : t('cashflow.form.titleCreatePayment')
  return (
    <FormShell title={title}>
      <VoucherFormBody key={`new-${String(kind)}`} voucher={null} kind={kind} />
    </FormShell>
  )
}

function ExistingVoucherPage({ id }: { readonly id: string }): ReactElement {
  const { t } = useI18n()
  const query = useCashVoucher(id)

  if (query.isPending) {
    return (
      <FormShell title={t('cashflow.form.titleCash')}>
        <p className="text-app text-text-muted">{t('common.loading')}</p>
      </FormShell>
    )
  }
  if (query.isError) {
    const message =
      query.error instanceof ApiError
        ? translateErrorCode(t, query.error.errorCode)
        : t('error.transport.unreachable')
    return (
      <FormShell title={t('cashflow.form.titleCash')}>
        <Alert tone="error">{message}</Alert>
      </FormShell>
    )
  }
  return (
    <FormShell title={t('cashflow.form.titleEdit', { no: query.data.voucher_no })}>
      <VoucherFormBody key={query.data.id} voucher={query.data} kind={query.data.kind} />
    </FormShell>
  )
}

const DIMENSION_KEYS = DIMENSION_COLUMNS.map((column) => column.key)

function VoucherFormBody({
  voucher,
  kind,
}: {
  readonly voucher: CashVoucherOut | null
  readonly kind: number
}): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const { readOnly, datasetCode } = useSession()
  const access = useAccess()
  const queryClient = useQueryClient()

  const [postingDate, setPostingDate] = useState(() => voucher?.posting_date ?? todayIso())
  const [documentDate, setDocumentDate] = useState(
    () => voucher?.document_date ?? voucher?.posting_date ?? todayIso(),
  )
  const [documentDateTouched, setDocumentDateTouched] = useState(voucher !== null)
  const [operationCode, setOperationCode] = useState(() => voucher?.operation_code ?? '')
  const [description, setDescription] = useState(() => voucher?.description ?? '')
  const [payerReceiverName, setPayerReceiverName] = useState(
    () => voucher?.payer_receiver_name ?? '',
  )
  const [currencyCode, setCurrencyCode] = useState(() => voucher?.currency_code ?? 'VND')
  const [exchangeRate, setExchangeRate] = useState(() => voucher?.exchange_rate ?? '1')
  const [cashflowActivity, setCashflowActivity] = useState(() =>
    voucher?.cashflow_activity === null || voucher?.cashflow_activity === undefined
      ? ''
      : String(voucher.cashflow_activity),
  )
  const [cashAccount, setCashAccount] = useState<LookupOption | null>(null)
  const [partner, setPartner] = useState<LookupOption | null>(null)
  const [rows, setRows] = useState<PairLineRow[]>(() => [emptyPairLineRow()])
  const [settlementAmounts, setSettlementAmounts] = useState<Readonly<Record<string, string>>>({})
  const [error, setError] = useState<string | null>(null)
  const [violations, setViolations] = useState<readonly Violation[]>([])
  const [failedIntent, setFailedIntent] = useState<'create' | 'post' | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [idempotencyKey] = useState(newIdempotencyKey)

  const documentType = kind === KIND_RECEIPT ? 'PT' : 'PC'
  const operations = useAutoPostingOperations(documentType, postingDate)
  const operationItems = operations.data?.items ?? []
  const selectedOperation = operationItems.find(
    (operation) => operation.operation_code === operationCode,
  )

  // Loại đối tượng: theo nghiệp vụ đã chọn; không gợi ý thì phiếu thu mặc định
  // khách hàng, phiếu chi mặc định nhà cung cấp (chiều đối trừ BR-QUY).
  const partnerKind =
    selectedOperation?.partner_kind ?? (kind === KIND_RECEIPT ? 0 : 1)
  const partnerSlug = partnerKind === 2 ? 'employees' : 'partners'
  const partnerLookup = useMasterSearchLookup(
    partnerSlug,
    voucher !== null && voucher.partner_id !== null && voucher.partner_id !== undefined
      ? [voucher.partner_id]
      : [],
  )

  const requiredAccountIds = [
    ...(voucher?.lines.flatMap((line) =>
      [line.debit_account_id, line.credit_account_id].filter(
        (value): value is number => value !== null,
      ),
    ) ?? []),
    ...(voucher === null ? [] : [voucher.cash_account_id]),
  ]
  const accountLookup = useAccountLookup(postingDate, requiredAccountIds)
  const dimensionLookups = useDimensionLookups()
  const [hydrated, setHydrated] = useState(false)

  // Dựng lại form từ chứng từ đã lưu đúng MỘT lần khi các lượt tra xong —
  // "điều chỉnh state trong thân render", cùng lý do với form GLE.
  if (
    voucher !== null &&
    !hydrated &&
    !accountLookup.isLoading &&
    !dimensionLookups.isLoading &&
    !partnerLookup.isLoading
  ) {
    setHydrated(true)
    setRows(
      voucher.lines.length === 0
        ? [emptyPairLineRow()]
        : voucher.lines.map((line) =>
            buildRowFromPairLine(line, accountLookup.maps, dimensionLookups.options),
          ),
    )
    const cash = accountLookup.maps.byId.get(voucher.cash_account_id)
    if (cash !== undefined) {
      setCashAccount({ id: cash.id, code: cash.code, label: cash.name })
    }
    if (voucher.partner_id !== null && voucher.partner_id !== undefined) {
      setPartner(partnerLookup.byId.get(voucher.partner_id) ?? null)
    }
    if (voucher.settlements.length > 0) {
      setSettlementAmounts(
        Object.fromEntries(
          voucher.settlements.map((row) => [
            settlementKey(row.target_kind, row.target_id),
            row.amount_fc,
          ]),
        ),
      )
    }
  }

  const createMutation = useCreateCashVoucher()
  const updateMutation = useUpdateCashVoucher(voucher?.id ?? '')
  const actions = useVoucherActions()

  const busy =
    createMutation.isPending ||
    updateMutation.isPending ||
    actions.post.isPending ||
    actions.unpost.isPending ||
    actions.remove.isPending

  function fail(caught: unknown, intent: 'create' | 'post' | null = null): void {
    setFailedIntent(intent)
    if (caught instanceof ApiError) {
      setError(translateErrorCode(t, caught.errorCode))
      setViolations(extractViolations(caught.problem))
      if (caught.status === 409 && voucher !== null) {
        void queryClient.invalidateQueries({
          queryKey: ['cash-voucher', datasetCode, voucher.id],
        })
      }
    } else {
      setError(t('error.transport.unreachable'))
      setViolations([])
    }
  }

  function goToList(): void {
    void navigate('/tien-vao-tien-ra/giao-dich')
  }

  function handleCommit(changes: readonly DataGridChange[]): void {
    setRows((current) => applyPairLineChanges(current, changes, DIMENSION_KEYS))
    for (const change of changes) {
      if (change.columnKey === 'debit_account' || change.columnKey === 'credit_account') {
        accountLookup.resolve(change.value)
      }
    }
  }

  function handleOperationChange(code: string): void {
    setOperationCode(code)
    const operation = operationItems.find((item) => item.operation_code === code)
    if (operation === undefined) {
      return
    }
    // Điền sẵn cặp Nợ/Có vào DÒNG ĐẦU khi nó còn trắng cả hai bên TK — người
    // dùng đã gõ gì thì không ghi đè.
    setRows((current) => {
      const first = current[0]
      if (first === undefined || first.debitCode.trim() !== '' || first.creditCode.trim() !== '') {
        return current
      }
      const next = [...current]
      next[0] = {
        ...first,
        debitCode: operation.debit_account_code ?? '',
        creditCode: operation.credit_account_code ?? '',
      }
      return next
    })
    if (operation.debit_account_code !== null) {
      accountLookup.resolve(operation.debit_account_code)
    }
    if (operation.credit_account_code !== null) {
      accountLookup.resolve(operation.credit_account_code)
    }
  }

  const cashAccountOptions: LookupOption[] = [...accountLookup.maps.byCode.values()]
    .filter((account) => account.code.startsWith(CASH_ACCOUNT_PREFIX) && !account.is_summary)
    .map((account) => ({ id: account.id, code: account.code, label: account.name }))

  const requiredDimensions = new Set<string>()
  for (const row of rows) {
    for (const code of [row.debitCode, row.creditCode]) {
      const account = accountLookup.maps.byCode.get(code.trim().toLowerCase())
      for (const value of account?.detail_tracking ?? []) {
        requiredDimensions.add(value)
      }
    }
  }
  const visibleDimensionColumns = DIMENSION_COLUMNS.filter((column) =>
    column.values.some((value) => requiredDimensions.has(value)),
  )
  const columns = buildPairLineColumns(t, accountLookup.maps, visibleDimensionColumns)

  const branchId = voucher !== null ? voucher.branch_id : (access.data?.acting_branch_id ?? null)

  // Khối đối trừ chỉ có nghĩa khi đối tác là KH (phiếu thu) hoặc NCC (phiếu
  // chi) — nhân viên và các nghiệp vụ không đối tác không có công nợ để trừ.
  const settlementSide = kind === KIND_RECEIPT ? ('receivable' as const) : ('payable' as const)
  const expectedSettlementKind = kind === KIND_RECEIPT ? 0 : 1
  const settlementQuery =
    partner !== null && partnerKind === expectedSettlementKind && branchId !== null
      ? {
          basePath: '/api/v1/cash-book' as const,
          side: settlementSide,
          partnerKind,
          partnerId: partner.id,
          branchId,
          asOf: postingDate,
        }
      : null

  function buildSettlements(): { target_kind: number; target_id: string; amount_fc: string }[] {
    return Object.entries(settlementAmounts)
      .filter(([, amount]) => amount.trim() !== '')
      .map(([key, amount]) => {
        const separator = key.indexOf(':')
        return {
          target_kind: Number.parseInt(key.slice(0, separator), 10),
          target_id: key.slice(separator + 1),
          amount_fc: amount.trim(),
        }
      })
  }

  function handleSave(acknowledgeWarnings = false): void {
    setError(null)
    setViolations([])
    setFailedIntent(null)

    if (postingDate.trim() === '') {
      setError(t('cashflow.form.error.postingDateRequired'))
      return
    }
    if (operationCode === '') {
      setError(t('cashflow.form.error.operationRequired'))
      return
    }
    if (cashAccount === null) {
      setError(t('cashflow.form.error.cashAccountRequired'))
      return
    }
    if (branchId === null) {
      setError(t('cashflow.form.branchMissing'))
      return
    }

    const resolved = resolvePairLines(rows, accountLookup.maps, dimensionLookups.options, t)
    if (resolved.errors.length > 0) {
      setError(resolved.errors.join(' '))
      return
    }
    if (resolved.lines.length === 0) {
      setError(t('cashflow.form.linesRequired'))
      return
    }

    const body: Record<string, unknown> = {
      kind,
      operation_code: operationCode,
      cash_account_id: cashAccount.id,
      branch_id: branchId,
      document_date: documentDate.trim() === '' ? postingDate : documentDate,
      posting_date: postingDate,
      currency_code: currencyCode.trim() === '' ? 'VND' : currencyCode.trim(),
      exchange_rate: exchangeRate.trim() === '' ? '1' : exchangeRate.trim(),
      lines: resolved.lines,
      settlements: buildSettlements(),
    }
    if (partner !== null) {
      body.partner_id = partner.id
      body.partner_kind = partnerKind
    }
    if (payerReceiverName.trim() !== '') {
      body.payer_receiver_name = payerReceiverName.trim()
    }
    if (description.trim() !== '') {
      body.description = description.trim()
    }
    if (cashflowActivity.trim() !== '') {
      const parsed = Number.parseInt(cashflowActivity, 10)
      if (!Number.isNaN(parsed)) {
        body.cashflow_activity = parsed
      }
    }

    if (voucher === null) {
      createMutation.mutate(
        { body: body as unknown as CashVoucherIn, idempotencyKey, acknowledgeWarnings },
        {
          onSuccess: goToList,
          onError: (caught) => {
            fail(caught, 'create')
          },
        },
      )
      return
    }
    updateMutation.mutate({ ...body, row_version: voucher.row_version } as unknown as CashVoucherUpdate, {
      onSuccess: goToList,
      onError: (caught) => {
        fail(caught)
      },
    })
  }

  function handleAcknowledge(): void {
    if (failedIntent === 'create') {
      handleSave(true)
      return
    }
    if (failedIntent === 'post' && voucher !== null) {
      setError(null)
      setViolations([])
      setFailedIntent(null)
      actions.post.mutate(
        { id: voucher.id, idempotencyKey: newIdempotencyKey(), acknowledgeWarnings: true },
        {
          onSuccess: goToList,
          onError: (caught) => {
            fail(caught, 'post')
          },
        },
      )
    }
  }

  const hasAdvancedValues =
    voucher !== null &&
    (documentDate !== postingDate ||
      payerReceiverName !== '' ||
      currencyCode !== 'VND' ||
      exchangeRate !== '1' ||
      cashflowActivity !== '')

  return (
    <div className="flex flex-col gap-4">
      {error !== null && (
        <JournalViolationsAlert
          error={error}
          violations={violations}
          busy={busy}
          onAcknowledge={failedIntent === null ? undefined : handleAcknowledge}
        />
      )}

      <CashVoucherHeaderFields
        postingDate={postingDate}
        onPostingDateChange={(value) => {
          setPostingDate(value)
          if (!documentDateTouched) {
            setDocumentDate(value)
          }
        }}
        operationCode={operationCode}
        operations={operationItems}
        onOperationChange={handleOperationChange}
        cashAccount={cashAccount}
        cashAccountOptions={cashAccountOptions}
        onCashAccountChange={setCashAccount}
        partner={partner}
        partnerOptions={partnerLookup.options}
        onPartnerChange={setPartner}
        onPartnerQueryChange={partnerLookup.searchFor}
        partnerRequired={selectedOperation?.requires_partner ?? false}
        description={description}
        onDescriptionChange={setDescription}
        documentDate={documentDate}
        onDocumentDateChange={(value) => {
          setDocumentDate(value)
          setDocumentDateTouched(true)
        }}
        payerReceiverName={payerReceiverName}
        onPayerReceiverNameChange={setPayerReceiverName}
        currencyCode={currencyCode}
        onCurrencyCodeChange={setCurrencyCode}
        exchangeRate={exchangeRate}
        onExchangeRateChange={setExchangeRate}
        cashflowActivity={cashflowActivity}
        onCashflowActivityChange={setCashflowActivity}
        defaultAdvancedOpen={hasAdvancedValues}
      />

      <DataGrid
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption={t('cashflow.line.caption')}
        cellLabel={(header, rowNumber) =>
          t('cashflow.line.cellLabel', { header, row: String(rowNumber) })
        }
        onCommit={handleCommit}
      />

      <SettlementSection
        query={settlementQuery}
        amounts={settlementAmounts}
        onAmountChange={(key, value) => {
          setSettlementAmounts((current) => ({ ...current, [key]: value }))
        }}
        disabled={readOnly || busy}
      />

      <JournalVoucherActionsFooter
        voucherStatus={voucher?.status ?? null}
        voucherId={voucher?.id ?? null}
        documentType={voucher?.document_type ?? documentType}
        readOnly={readOnly}
        busy={busy}
        confirmDelete={confirmDelete}
        onCancel={goToList}
        onSave={() => {
          handleSave()
        }}
        onPost={() => {
          if (voucher === null) {
            return
          }
          setError(null)
          actions.post.mutate(
            { id: voucher.id, idempotencyKey: newIdempotencyKey() },
            {
              onSuccess: goToList,
              onError: (caught) => {
                fail(caught, 'post')
              },
            },
          )
        }}
        onUnpost={() => {
          if (voucher === null) {
            return
          }
          setError(null)
          actions.unpost.mutate(
            { id: voucher.id, idempotencyKey: newIdempotencyKey() },
            { onSuccess: goToList, onError: (caught) => { fail(caught) } },
          )
        }}
        onDelete={() => {
          if (voucher === null) {
            return
          }
          if (!confirmDelete) {
            setConfirmDelete(true)
            return
          }
          setError(null)
          actions.remove.mutate(voucher.id, {
            onSuccess: goToList,
            onError: (caught) => {
              fail(caught)
            },
          })
        }}
      />
    </div>
  )
}
