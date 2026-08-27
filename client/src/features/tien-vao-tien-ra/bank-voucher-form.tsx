/**
 * Form chứng từ tiền gửi (bước 19) — `/tien-vao-tien-ra/giao-dich/ngan-hang/moi`
 * và `.../ngan-hang/:id`. Bốn loại một thân (BC/UNC/SEC/CTNB — quyết định 6C):
 * loại chọn bằng Seg lúc tạo, BẤT BIẾN sau khi cất (cùng luật chi nhánh).
 *
 * Chuyển nội bộ (CTNB) khác ba loại kia: không nghiệp vụ định khoản, không đối
 * tác, không đối trừ — đòi TK ngân hàng ĐÍCH cùng tiền tệ; các luật đó server
 * giữ, form chỉ ẩn/hiện ô cho khỏi mời nhập thứ sẽ bị từ chối.
 */

import type { ReactElement, ReactNode } from 'react'
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'

import type { DataGridChange, LookupOption } from '@/design-system/components'
import { Alert, DataGrid } from '@/design-system/components'
import { useAccess } from '@/lib/access'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useLookupOptions } from '@/features/danh-muc-thiet-lap/use-lookup-options'
import { DIMENSION_COLUMNS } from '@/features/so-sach-thue/dimension-config'
import { JournalViolationsAlert } from '@/features/so-sach-thue/journal-violations-alert'
import { extractViolations, type Violation } from '@/features/so-sach-thue/journal-violations'
import { JournalVoucherActionsFooter } from '@/features/so-sach-thue/journal-voucher-actions-footer'
import { todayIso } from '@/features/so-sach-thue/local-date'
import { useAccountLookup } from '@/features/so-sach-thue/use-account-lookup'
import {
  requiredDimensionIdsOf,
  useDimensionLookups,
} from '@/features/so-sach-thue/use-dimension-lookups'
import { useVoucherActions } from '@/features/so-sach-thue/use-voucher-actions'

import {
  BANK_KIND_CHEQUE,
  BANK_KIND_CREDIT_ADVICE,
  BANK_KIND_INTERNAL_TRANSFER,
  BankVoucherHeaderFields,
} from './bank-voucher-header-fields'
import { FeatureNav } from './feature-nav'
import { buildPairLineColumns } from './pair-line-columns'
import { buildRowFromPairLine } from './pair-line-hydrate'
import { resolvePairLines } from './pair-line-resolve'
import { applyPairLineChanges, emptyPairLineRow, type PairLineRow } from './pair-line-types'
import { SettlementSection, settlementKey } from './settlement-section'
import { useAutoPostingOperations } from './use-auto-posting'
import {
  useBankVoucher,
  useCreateBankVoucher,
  useUpdateBankVoucher,
  type BankVoucherIn,
  type BankVoucherOut,
  type BankVoucherUpdate,
} from './use-bank-voucher'
import { useMasterSearchLookup } from './use-master-search-lookup'

/** Mã loại chứng từ theo `kind` — khớp `modules/bank/service.py`. */
const DOCUMENT_TYPE_BY_KIND: readonly string[] = ['BC', 'UNC', 'SEC', 'CTNB']

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

export function BankVoucherForm(): ReactElement {
  const { id } = useParams<{ id?: string }>()
  return id === undefined ? <NewVoucherPage /> : <ExistingVoucherPage id={id} />
}

function NewVoucherPage(): ReactElement {
  const { t } = useI18n()
  return (
    <FormShell title={t('cashflow.bank.titleCreate')}>
      <VoucherFormBody key="new" voucher={null} />
    </FormShell>
  )
}

function ExistingVoucherPage({ id }: { readonly id: string }): ReactElement {
  const { t } = useI18n()
  const query = useBankVoucher(id)

  if (query.isPending) {
    return (
      <FormShell title={t('cashflow.bank.title')}>
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
      <FormShell title={t('cashflow.bank.title')}>
        <Alert tone="error">{message}</Alert>
      </FormShell>
    )
  }
  return (
    <FormShell title={t('cashflow.form.titleEdit', { no: query.data.voucher_no })}>
      <VoucherFormBody key={query.data.id} voucher={query.data} />
    </FormShell>
  )
}

const DIMENSION_KEYS = DIMENSION_COLUMNS.map((column) => column.key)

function VoucherFormBody({
  voucher,
}: {
  readonly voucher: BankVoucherOut | null
}): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const { readOnly, datasetCode } = useSession()
  const access = useAccess()
  const queryClient = useQueryClient()

  const [kind, setKind] = useState(() => voucher?.kind ?? BANK_KIND_CREDIT_ADVICE)
  const [postingDate, setPostingDate] = useState(() => voucher?.posting_date ?? todayIso())
  const [documentDate, setDocumentDate] = useState(
    () => voucher?.document_date ?? voucher?.posting_date ?? todayIso(),
  )
  const [documentDateTouched, setDocumentDateTouched] = useState(voucher !== null)
  const [operationCode, setOperationCode] = useState(() => voucher?.operation_code ?? '')
  const [description, setDescription] = useState(() => voucher?.description ?? '')
  const [beneficiaryName, setBeneficiaryName] = useState(() => voucher?.beneficiary_name ?? '')
  const [beneficiaryAccountNo, setBeneficiaryAccountNo] = useState(
    () => voucher?.beneficiary_account_no ?? '',
  )
  const [beneficiaryBankName, setBeneficiaryBankName] = useState(
    () => voucher?.beneficiary_bank_name ?? '',
  )
  const [chequeNo, setChequeNo] = useState(() => voucher?.cheque_no ?? '')
  const [chequeDate, setChequeDate] = useState(() => voucher?.cheque_date ?? '')
  const [referenceNo, setReferenceNo] = useState(() => voucher?.reference_no ?? '')
  const [currencyCode, setCurrencyCode] = useState(() => voucher?.currency_code ?? 'VND')
  const [exchangeRate, setExchangeRate] = useState(() => voucher?.exchange_rate ?? '1')
  const [cashflowActivity, setCashflowActivity] = useState(() =>
    voucher?.cashflow_activity === null || voucher?.cashflow_activity === undefined
      ? ''
      : String(voucher.cashflow_activity),
  )
  const [bankAccount, setBankAccount] = useState<LookupOption | null>(null)
  const [counterBankAccount, setCounterBankAccount] = useState<LookupOption | null>(null)
  const [partner, setPartner] = useState<LookupOption | null>(null)
  const [rows, setRows] = useState<PairLineRow[]>(() => [emptyPairLineRow()])
  const [settlementAmounts, setSettlementAmounts] = useState<Readonly<Record<string, string>>>({})
  const [error, setError] = useState<string | null>(null)
  const [violations, setViolations] = useState<readonly Violation[]>([])
  const [failedIntent, setFailedIntent] = useState<'create' | 'post' | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [idempotencyKey] = useState(newIdempotencyKey)

  const transfer = kind === BANK_KIND_INTERNAL_TRANSFER
  const documentType = DOCUMENT_TYPE_BY_KIND[kind] ?? 'BC'
  const operations = useAutoPostingOperations(transfer ? null : documentType, postingDate)
  const operationItems = operations.data?.items ?? []
  const selectedOperation = operationItems.find(
    (operation) => operation.operation_code === operationCode,
  )

  // Loại đối tượng là STATE, không suy từ danh sách operations (review 6F-1
  // C-1) — form SỬA lấy từ `voucher.partner_kind`; nghiệp vụ chỉ gợi ý khi
  // người dùng chọn.
  const [partnerKind, setPartnerKind] = useState<number>(
    () => voucher?.partner_kind ?? (kind === BANK_KIND_CREDIT_ADVICE ? 0 : 1),
  )
  const partnerSlug = partnerKind === 2 ? 'employees' : 'partners'
  const partnerLookup = useMasterSearchLookup(
    partnerSlug,
    voucher !== null && voucher.partner_id !== null && voucher.partner_id !== undefined
      ? [voucher.partner_id]
      : [],
  )

  // Danh mục TK ngân hàng doanh nghiệp là danh mục NHỎ (thứ 21, lát 6A) — nạp
  // trọn bằng cơ chế sẵn có của nhóm 07.
  const bankAccounts = useLookupOptions('company_bank_accounts')
  const bankAccountOptions = bankAccounts.data ?? []

  const requiredAccountIds =
    voucher?.lines.flatMap((line) =>
      [line.debit_account_id, line.credit_account_id].filter(
        (value): value is number => value !== null,
      ),
    ) ?? []
  const accountLookup = useAccountLookup(postingDate, requiredAccountIds)
  // Danh mục chiều tra server-side (nợ M-B 6F-1) — id trên dòng cũ tra bù
  // trước khi dựng lưới, cùng lý do với `requiredAccountIds` của TK.
  const dimensionLookups = useDimensionLookups(requiredDimensionIdsOf(voucher?.lines ?? []))
  const [hydrated, setHydrated] = useState(false)

  if (
    voucher !== null &&
    !hydrated &&
    !accountLookup.isLoading &&
    !dimensionLookups.isLoading &&
    !partnerLookup.isLoading &&
    !bankAccounts.isPending
  ) {
    setHydrated(true)
    setRows(
      voucher.lines.length === 0
        ? [emptyPairLineRow()]
        : voucher.lines.map((line) =>
            buildRowFromPairLine(line, accountLookup.maps, dimensionLookups.options),
          ),
    )
    setBankAccount(bankAccountOptions.find((option) => option.id === voucher.bank_account_id) ?? null)
    if (voucher.counter_bank_account_id !== null) {
      setCounterBankAccount(
        bankAccountOptions.find((option) => option.id === voucher.counter_bank_account_id) ?? null,
      )
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

  const createMutation = useCreateBankVoucher()
  const updateMutation = useUpdateBankVoucher(voucher?.id ?? '')
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
          queryKey: ['bank-voucher', datasetCode, voucher.id],
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
    // Nghiệp vụ gợi ý LOẠI đối tượng; đổi loại thì đối tác + số đối trừ đã gõ
    // không còn nghĩa (review 6F-1 H-1).
    const suggestedKind = operation.partner_kind ?? (kind === BANK_KIND_CREDIT_ADVICE ? 0 : 1)
    if (suggestedKind !== partnerKind) {
      setPartnerKind(suggestedKind)
      setPartner(null)
      setSettlementAmounts({})
    }
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

  const settlementSide =
    kind === BANK_KIND_CREDIT_ADVICE ? ('receivable' as const) : ('payable' as const)
  const expectedSettlementKind = kind === BANK_KIND_CREDIT_ADVICE ? 0 : 1
  const settlementQuery =
    !transfer && partner !== null && partnerKind === expectedSettlementKind && branchId !== null
      ? {
          basePath: '/api/v1/bank' as const,
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
    if (!transfer && operationCode === '') {
      setError(t('cashflow.form.error.operationRequired'))
      return
    }
    if (bankAccount === null) {
      setError(t('cashflow.bank.error.accountRequired'))
      return
    }
    if (transfer && counterBankAccount === null) {
      setError(t('cashflow.bank.error.counterAccountRequired'))
      return
    }
    // Lớp báo sớm cho nghiệp vụ đòi đối tác — server vẫn là cổng chính
    // (review 6F-1 C-1).
    if (!transfer && (selectedOperation?.requires_partner ?? false) && partner === null) {
      setError(t('cashflow.form.error.partnerRequired'))
      return
    }
    if (branchId === null) {
      setError(t('cashflow.form.branchMissing'))
      return
    }

    void submitResolvedLines(branchId, bankAccount.id, acknowledgeWarnings)
  }

  // Tách async khỏi `handleSave` vì lượt rà mã chiều có thể phải hỏi server
  // (hai-lượt-rà, nợ M-B 6F-1): mã ngoài trang seed được tra `search=` rồi rà
  // lại — mã sai thật thì lượt hai báo đúng lỗi cũ, không có đường lỗi mới.
  async function submitResolvedLines(
    branchId: number,
    bankAccountId: number,
    acknowledgeWarnings: boolean,
  ): Promise<void> {
    let resolved = resolvePairLines(rows, accountLookup.maps, dimensionLookups.options, t)
    if (resolved.missing.length > 0) {
      const mergedOptions = await dimensionLookups.resolveMissingCodes(resolved.missing)
      resolved = resolvePairLines(rows, accountLookup.maps, mergedOptions, t)
    }
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
      bank_account_id: bankAccountId,
      branch_id: branchId,
      document_date: documentDate.trim() === '' ? postingDate : documentDate,
      posting_date: postingDate,
      currency_code: currencyCode.trim() === '' ? 'VND' : currencyCode.trim(),
      exchange_rate: exchangeRate.trim() === '' ? '1' : exchangeRate.trim(),
      lines: resolved.lines,
      settlements: transfer ? [] : buildSettlements(),
    }
    if (transfer) {
      body.counter_bank_account_id = counterBankAccount?.id ?? null
    } else {
      body.operation_code = operationCode
      if (partner !== null) {
        body.partner_id = partner.id
        body.partner_kind = partnerKind
      }
    }
    if (beneficiaryName.trim() !== '') {
      body.beneficiary_name = beneficiaryName.trim()
    }
    if (beneficiaryAccountNo.trim() !== '') {
      body.beneficiary_account_no = beneficiaryAccountNo.trim()
    }
    if (beneficiaryBankName.trim() !== '') {
      body.beneficiary_bank_name = beneficiaryBankName.trim()
    }
    if (chequeNo.trim() !== '') {
      body.cheque_no = chequeNo.trim()
    }
    if (chequeDate.trim() !== '') {
      body.cheque_date = chequeDate
    }
    if (referenceNo.trim() !== '') {
      body.reference_no = referenceNo.trim()
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
        { body: body as unknown as BankVoucherIn, idempotencyKey, acknowledgeWarnings },
        {
          onSuccess: goToList,
          onError: (caught) => {
            fail(caught, 'create')
          },
        },
      )
      return
    }
    updateMutation.mutate({ ...body, row_version: voucher.row_version } as unknown as BankVoucherUpdate, {
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
      beneficiaryName !== '' ||
      chequeNo !== '' ||
      referenceNo !== '' ||
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

      <BankVoucherHeaderFields
        kind={kind}
        onKindChange={(value) => {
          setKind(value)
          // Nghiệp vụ thuộc TỪNG loại chứng từ — đổi loại là chọn lại từ đầu.
          // State riêng của loại cũ cũng phải theo: số séc trên chứng từ không
          // phải séc bị server 422 dù ô đã ẩn (review 6F-1 M-D), số đối trừ đổi
          // chiều thu↔chi thành dòng vô hình (H-1).
          setOperationCode('')
          setPartnerKind(value === BANK_KIND_CREDIT_ADVICE ? 0 : 1)
          setPartner(null)
          setSettlementAmounts({})
          setChequeNo('')
          setChequeDate('')
          setBeneficiaryName('')
          setBeneficiaryAccountNo('')
          setBeneficiaryBankName('')
        }}
        kindLocked={voucher !== null}
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
        bankAccount={bankAccount}
        bankAccountOptions={bankAccountOptions}
        onBankAccountChange={setBankAccount}
        counterBankAccount={counterBankAccount}
        onCounterBankAccountChange={setCounterBankAccount}
        partner={partner}
        partnerOptions={partnerLookup.options}
        onPartnerChange={(option) => {
          setPartner(option)
          // Đổi đối tác là đổi danh sách hóa đơn — không gửi dòng đối trừ vô
          // hình của đối tác cũ (review 6F-1 H-1).
          setSettlementAmounts({})
        }}
        onPartnerQueryChange={partnerLookup.searchFor}
        partnerRequired={selectedOperation?.requires_partner ?? false}
        description={description}
        onDescriptionChange={setDescription}
        documentDate={documentDate}
        onDocumentDateChange={(value) => {
          setDocumentDate(value)
          setDocumentDateTouched(true)
        }}
        beneficiaryName={beneficiaryName}
        onBeneficiaryNameChange={setBeneficiaryName}
        beneficiaryAccountNo={beneficiaryAccountNo}
        onBeneficiaryAccountNoChange={setBeneficiaryAccountNo}
        beneficiaryBankName={beneficiaryBankName}
        onBeneficiaryBankNameChange={setBeneficiaryBankName}
        chequeNo={chequeNo}
        onChequeNoChange={setChequeNo}
        chequeDate={chequeDate}
        onChequeDateChange={setChequeDate}
        referenceNo={referenceNo}
        onReferenceNoChange={setReferenceNo}
        currencyCode={currencyCode}
        onCurrencyCodeChange={setCurrencyCode}
        exchangeRate={exchangeRate}
        onExchangeRateChange={setExchangeRate}
        cashflowActivity={cashflowActivity}
        onCashflowActivityChange={setCashflowActivity}
        defaultAdvancedOpen={hasAdvancedValues || kind === BANK_KIND_CHEQUE}
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
