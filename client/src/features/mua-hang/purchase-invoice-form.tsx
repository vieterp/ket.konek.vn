/**
 * Form hóa đơn mua hàng (design `#mua-form`, U2) — `/mua-hang/chung-tu/moi?kind=0..4`
 * và `/mua-hang/chung-tu/:id`.
 *
 * Ba trường bắt buộc hiện sẵn (NCC · Ngày · Nghiệp vụ), lưới "Mua cái gì",
 * khối chi phí mua phân bổ ngay trên form, "Mở rộng" thu gọn, cột phải ba thẻ
 * tóm tắt. `kind=4` (trả lại hàng) thêm khối đối trừ hóa đơn gốc — cùng
 * `SettlementSection` với phiếu chi, đọc `/purchase/open-invoices`.
 *
 * Cùng khung xương `tien-vao-tien-ra/cash-voucher-form.tsx`: định tuyến theo
 * `:id`, form dựng từ dữ liệu ĐÃ tải (`key={voucher.id}`), lưới hydrate đúng
 * một lần khi các lượt tra danh mục xong, cảnh báo FR-SYS-062 (ngưỡng nợ NCC
 * của 7B) quay về dạng 422 toàn-cảnh-báo → băng "Vẫn ghi sổ?".
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

import { useMasterSearchLookup } from '@/features/danh-muc-thiet-lap/use-master-search-lookup'
import { usePartnerOverview } from '@/features/danh-muc-thiet-lap/use-partner'
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
import { SettlementSection, settlementKey } from '@/features/tien-vao-tien-ra/settlement-section'
import {
  useAutoPostingOperations,
  type AutoPostingOperation,
} from '@/features/tien-vao-tien-ra/use-auto-posting'

import { FeatureNav } from './feature-nav'
import { FormCard } from './form-card'
import { ALLOCATION_BY_VALUE, ALLOCATION_MANUAL, LandedCostSection } from './landed-cost-section'
import { PurchaseHeaderFields } from './purchase-header-fields'
import { buildPurchaseLineColumns } from './purchase-line-columns'
import { buildRowFromLandedCost, buildRowFromPurchaseLine } from './purchase-line-hydrate'
import {
  PURCHASE_DIMENSION_COLUMNS,
  resolveLandedCosts,
  resolvePurchaseLines,
} from './purchase-line-resolve'
import {
  applyLandedCostChanges,
  applyPurchaseLineChanges,
  emptyLandedCostRow,
  emptyPurchaseLineRow,
  type LandedCostRow,
  type PurchaseLineRow,
} from './purchase-line-types'
import {
  PURCHASE_KIND_GOODS,
  PURCHASE_KIND_LABEL_KEYS,
  PURCHASE_KIND_RETURN,
  VENDOR_INVOICE_RECEIVED,
} from './purchase-row-status'
import { PurchaseSummaryCards } from './purchase-summary-cards'
import {
  useCreatePurchaseInvoice,
  usePurchaseInvoice,
  useUpdatePurchaseInvoice,
  type PurchaseInvoiceIn,
  type PurchaseInvoiceOut,
  type PurchaseInvoiceUpdate,
} from './use-purchase-invoices'

const DOCUMENT_TYPE = 'PUR'
/** Khớp `PartnerKind.VENDOR` phía server. */
const VENDOR_PARTNER_KIND = 1
const DIMENSION_KEYS = PURCHASE_DIMENSION_COLUMNS.map((column) => column.key)

function FormShell({ title, children }: { readonly title: string; readonly children: ReactNode }): ReactElement {
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

function parseKind(raw: string | null): number {
  const parsed = raw === null ? Number.NaN : Number.parseInt(raw, 10)
  return parsed in PURCHASE_KIND_LABEL_KEYS ? parsed : PURCHASE_KIND_GOODS
}

export function PurchaseInvoiceForm(): ReactElement {
  const { id } = useParams<{ id?: string }>()
  return id === undefined ? <NewInvoicePage /> : <ExistingInvoicePage id={id} />
}

function NewInvoicePage(): ReactElement {
  const { t } = useI18n()
  const [searchParams] = useSearchParams()
  const kind = parseKind(searchParams.get('kind'))
  const kindLabel = t(PURCHASE_KIND_LABEL_KEYS[kind as keyof typeof PURCHASE_KIND_LABEL_KEYS])
  return (
    <FormShell title={t('purchase.form.titleCreate', { kind: kindLabel })}>
      <InvoiceFormBody key={`new-${String(kind)}`} voucher={null} kind={kind} />
    </FormShell>
  )
}

function ExistingInvoicePage({ id }: { readonly id: string }): ReactElement {
  const { t } = useI18n()
  const query = usePurchaseInvoice(id)
  if (query.isPending) {
    return (
      <FormShell title={t('purchase.form.title')}>
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
      <FormShell title={t('purchase.form.title')}>
        <Alert tone="error">{message}</Alert>
      </FormShell>
    )
  }
  return (
    <FormShell title={t('purchase.form.titleEdit', { no: query.data.voucher_no })}>
      <InvoiceFormBody key={query.data.id} voucher={query.data} kind={query.data.kind} />
    </FormShell>
  )
}

function optionOf(id: number, code: string, name: string): LookupOption {
  return { id, code, label: name }
}

function InvoiceFormBody({
  voucher,
  kind,
}: {
  readonly voucher: PurchaseInvoiceOut | null
  readonly kind: number
}): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const { readOnly, datasetCode } = useSession()
  const access = useAccess()
  const queryClient = useQueryClient()
  const isReturn = kind === PURCHASE_KIND_RETURN

  const [postingDate, setPostingDate] = useState(() => voucher?.posting_date ?? todayIso())
  const [documentDate, setDocumentDate] = useState(
    () => voucher?.document_date ?? voucher?.posting_date ?? todayIso(),
  )
  const [documentDateTouched, setDocumentDateTouched] = useState(voucher !== null)
  const [operationCode, setOperationCode] = useState(() => voucher?.operation_code ?? '')
  const [vendor, setVendor] = useState<LookupOption | null>(null)
  const [vendorInvoiceStatus, setVendorInvoiceStatus] = useState(
    () => voucher?.vendor_invoice_status ?? VENDOR_INVOICE_RECEIVED,
  )
  const [vendorInvoiceForm, setVendorInvoiceForm] = useState(() => voucher?.vendor_invoice_form ?? '')
  const [vendorInvoiceSerial, setVendorInvoiceSerial] = useState(
    () => voucher?.vendor_invoice_serial ?? '',
  )
  const [vendorInvoiceNo, setVendorInvoiceNo] = useState(() => voucher?.vendor_invoice_no ?? '')
  const [vendorInvoiceDate, setVendorInvoiceDate] = useState(() => voucher?.vendor_invoice_date ?? '')
  const [payableAccount, setPayableAccount] = useState<LookupOption | null>(null)
  const [paymentTermId, setPaymentTermId] = useState(() =>
    voucher?.payment_term_id === null || voucher?.payment_term_id === undefined
      ? ''
      : String(voucher.payment_term_id),
  )
  const [dueDate, setDueDate] = useState(() => voucher?.due_date ?? '')
  const [buyer, setBuyer] = useState<LookupOption | null>(null)
  // Người mua đã lưu mà danh mục không tra được (nhân viên đã xóa/ngừng) vẫn
  // phải vọng lại y nguyên khi PUT — chỉ khi người dùng chủ động đổi mới gửi
  // giá trị mới (review 7H-1 M-5).
  const [buyerTouched, setBuyerTouched] = useState(false)
  const [currencyCode, setCurrencyCode] = useState(() => voucher?.currency_code ?? 'VND')
  const [exchangeRate, setExchangeRate] = useState(() => voucher?.exchange_rate ?? '1')
  const [description, setDescription] = useState(() => voucher?.description ?? '')
  const [allocation, setAllocation] = useState(
    () => voucher?.landed_cost_allocation ?? ALLOCATION_BY_VALUE,
  )
  const [rows, setRows] = useState<PurchaseLineRow[]>(() => [emptyPurchaseLineRow()])
  const [costRows, setCostRows] = useState<LandedCostRow[]>(() => [emptyLandedCostRow()])
  const [settlementAmounts, setSettlementAmounts] = useState<Readonly<Record<string, string>>>({})
  const [error, setError] = useState<string | null>(null)
  const [violations, setViolations] = useState<readonly Violation[]>([])
  const [failedIntent, setFailedIntent] = useState<'create' | 'post' | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [idempotencyKey] = useState(newIdempotencyKey)

  const operations = useAutoPostingOperations(DOCUMENT_TYPE, postingDate)
  const operationItems = operations.data?.items ?? []
  const [operationDefaulted, setOperationDefaulted] = useState(voucher !== null)

  const vendorLookup = useMasterSearchLookup('partners', voucher === null ? [] : [voucher.vendor_id])
  const buyerLookup = useMasterSearchLookup(
    'employees',
    voucher?.buyer_id === null || voucher?.buyer_id === undefined ? [] : [voucher.buyer_id],
  )
  const paymentTerms = useMasterSearchLookup('payment_terms')
  const unitLookup = useMasterSearchLookup(
    'units_of_measure',
    voucher?.lines.flatMap((line) => (line.unit_id === null ? [] : [line.unit_id])) ?? [],
  )
  // Thẻ công nợ của NCC (BFF 7G-4) chỉ để lấy MST + điều khoản cho dòng gợi ý
  // dưới ô NCC; không có quyền danh mục thì ô vẫn dùng được, chỉ thiếu gợi ý.
  const vendorOverview = usePartnerOverview(vendor?.id ?? null)

  const requiredAccountIds = [
    ...(voucher?.lines.flatMap((line) =>
      [line.account_id, line.vat_account_id].filter((value): value is number => value !== null),
    ) ?? []),
    ...(voucher?.landed_costs.flatMap((cost) =>
      [cost.credit_account_id, cost.vat_account_id].filter(
        (value): value is number => value !== null,
      ),
    ) ?? []),
    ...(voucher === null ? [] : [voucher.payable_account_id]),
  ]
  const accountLookup = useAccountLookup(postingDate, requiredAccountIds)
  // Mặt hàng/kho/chiều trên dòng + NCC của từng khoản chi phí mua: id trên
  // chứng từ cũ tra bù trước khi dựng lưới (nợ M-B 6F-1).
  const dimensionLookups = useDimensionLookups(
    requiredDimensionIdsOf([
      ...(voucher?.lines ?? []),
      ...(voucher?.landed_costs.map((cost) => ({
        partner_kind: VENDOR_PARTNER_KIND,
        partner_id: cost.vendor_id,
      })) ?? []),
    ]),
  )
  const [hydrated, setHydrated] = useState(false)

  if (
    voucher !== null &&
    !hydrated &&
    !accountLookup.isLoading &&
    !dimensionLookups.isLoading &&
    !vendorLookup.isLoading &&
    !buyerLookup.isLoading &&
    !unitLookup.isLoading
  ) {
    setHydrated(true)
    setRows(
      voucher.lines.length === 0
        ? [emptyPurchaseLineRow()]
        : voucher.lines.map((line) =>
            buildRowFromPurchaseLine(
              line,
              accountLookup.maps,
              dimensionLookups.options,
              unitLookup.options,
            ),
          ),
    )
    setCostRows(
      voucher.landed_costs.length === 0
        ? [emptyLandedCostRow()]
        : voucher.landed_costs.map((cost) =>
            buildRowFromLandedCost(cost, accountLookup.maps, dimensionLookups.options['partners']),
          ),
    )
    const payable = accountLookup.maps.byId.get(voucher.payable_account_id)
    if (payable !== undefined) {
      setPayableAccount(optionOf(payable.id, payable.code, payable.name))
    }
    setVendor(vendorLookup.byId.get(voucher.vendor_id) ?? null)
    if (voucher.buyer_id !== null && voucher.buyer_id !== undefined) {
      setBuyer(buyerLookup.byId.get(voucher.buyer_id) ?? null)
    }
    if (voucher.settlements.length > 0) {
      setSettlementAmounts(
        Object.fromEntries(
          voucher.settlements.map((row) => [settlementKey(row.target_kind, row.target_id), row.amount_fc]),
        ),
      )
    }
  }

  const createMutation = useCreatePurchaseInvoice()
  const updateMutation = useUpdatePurchaseInvoice(voucher?.id ?? '')
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
        void queryClient.invalidateQueries({ queryKey: ['purchase', datasetCode, 'invoice', voucher.id] })
      }
    } else {
      setError(t('error.transport.unreachable'))
      setViolations([])
    }
  }

  function goToList(): void {
    void navigate('/mua-hang/chung-tu')
  }

  function handleCommit(changes: readonly DataGridChange[]): void {
    setRows((current) => applyPurchaseLineChanges(current, changes, DIMENSION_KEYS))
    for (const change of changes) {
      if (change.columnKey === 'account' || change.columnKey === 'vat_account') {
        accountLookup.resolve(change.value)
      }
    }
  }

  function handleCostCommit(changes: readonly DataGridChange[]): void {
    setCostRows((current) => applyLandedCostChanges(current, changes))
    for (const change of changes) {
      if (change.columnKey === 'credit_account' || change.columnKey === 'vat_account') {
        accountLookup.resolve(change.value)
      }
    }
  }

  /** Hai bên TK của một nghiệp vụ theo LOẠI chứng từ: hóa đơn thường Có TK phải trả; trả lại hàng đảo chiều — bên phải trả là bên Nợ. */
  function operationSides(operation: AutoPostingOperation): {
    readonly payableCode: string | null
    readonly lineCode: string | null
  } {
    return isReturn
      ? { payableCode: operation.debit_account_code, lineCode: operation.credit_account_code }
      : { payableCode: operation.credit_account_code, lineCode: operation.debit_account_code }
  }

  /**
   * Nghiệp vụ hợp với loại chứng từ (review 7H-1 M-3): bên phải trả của nó phải
   * là TK theo dõi NCC — tờ trả lại không được chọn nghiệp vụ mua rồi điền TK
   * lộn bên. Chưa tải hệ thống TK thì chưa lọc (danh sách đầy, không rỗng).
   */
  const applicableOperations = operationItems.filter((operation) => {
    // Nghiệp vụ ĐÃ LƯU trên chứng từ luôn có trong danh sách — kể cả khi gói
    // cấu hình đã đổi và nó không còn hợp — để ô không hiện trắng và PUT không
    // vô tình đổi nó (cùng lý do 6F-1 C-1).
    if (operation.operation_code === operationCode) {
      return true
    }
    const { payableCode } = operationSides(operation)
    if (payableCode === null) {
      return false
    }
    const account = accountLookup.maps.byCode.get(payableCode.toLowerCase())
    return account === undefined || (account.detail_tracking ?? []).includes('vendor')
  })

  /** Điền sẵn TK phải trả + TK dòng đầu khi chúng còn TRỐNG (luật 6F-1: không ghi đè thứ người dùng đã gõ). */
  function prefillFromOperation(operation: AutoPostingOperation): void {
    const { payableCode, lineCode } = operationSides(operation)
    if (payableCode !== null) {
      const account = accountLookup.maps.byCode.get(payableCode.toLowerCase())
      if (payableAccount === null && account !== undefined) {
        setPayableAccount(optionOf(account.id, account.code, account.name))
      }
    }
    if (lineCode !== null) {
      setRows((current) => {
        const first = current[0]
        if (first === undefined || first.accountCode.trim() !== '') {
          return current
        }
        const next = [...current]
        next[0] = { ...first, accountCode: lineCode }
        return next
      })
    }
  }

  // Form MỚI mà loại chứng từ chỉ có đúng MỘT nghiệp vụ hợp (tờ trả lại) thì
  // chọn sẵn — cùng lối "điều chỉnh state trong thân render một lần". Nhiều
  // nghiệp vụ thì để người dùng chọn: đoán sai còn tệ hơn để trống.
  if (
    !operationDefaulted &&
    operationCode === '' &&
    !accountLookup.isLoading &&
    operations.data !== undefined
  ) {
    setOperationDefaulted(true)
    const only = applicableOperations.length === 1 ? applicableOperations[0] : undefined
    if (only !== undefined) {
      setOperationCode(only.operation_code)
      prefillFromOperation(only)
    }
  }

  function handleOperationChange(code: string): void {
    setOperationCode(code)
    const operation = operationItems.find((item) => item.operation_code === code)
    if (operation === undefined) {
      return
    }
    const { payableCode, lineCode } = operationSides(operation)
    if (payableCode !== null) {
      accountLookup.resolve(payableCode)
    }
    if (lineCode !== null) {
      accountLookup.resolve(lineCode)
    }
    prefillFromOperation(operation)
  }

  const payableAccountOptions: LookupOption[] = [...accountLookup.maps.byCode.values()]
    .filter((account) => !account.is_summary && (account.detail_tracking ?? []).includes('vendor'))
    .map((account) => optionOf(account.id, account.code, account.name))

  const requiredDimensions = new Set<string>()
  for (const row of rows) {
    for (const code of [row.accountCode, row.vatAccountCode]) {
      const account = accountLookup.maps.byCode.get(code.trim().toLowerCase())
      for (const value of account?.detail_tracking ?? []) {
        requiredDimensions.add(value)
      }
    }
  }
  const visibleDimensionColumns = PURCHASE_DIMENSION_COLUMNS.filter((column) =>
    column.values.some((value) => requiredDimensions.has(value)),
  )
  const manualLandedCost = allocation === ALLOCATION_MANUAL
  const columns = buildPurchaseLineColumns({
    t,
    accounts: accountLookup.maps,
    items: dimensionLookups.options['items'] ?? [],
    visibleDimensionColumns,
    manualLandedCost,
  })

  const branchId = voucher !== null ? voucher.branch_id : (access.data?.acting_branch_id ?? null)

  const partnerInfo = vendorOverview.data?.partner
  const vendorHint =
    vendor === null || partnerInfo === undefined
      ? null
      : [
          partnerInfo.tax_code ? t('purchase.form.vendorHint.taxCode', { code: partnerInfo.tax_code }) : null,
          partnerInfo.payment_term_id === null || partnerInfo.payment_term_id === undefined
            ? null
            : t('purchase.form.vendorHint.term', {
                term:
                  paymentTerms.byId.get(partnerInfo.payment_term_id)?.label ??
                  String(partnerInfo.payment_term_id),
              }),
        ]
          .filter((part): part is string => part !== null)
          .join(' · ') || null

  const settlementQuery =
    isReturn && vendor !== null && branchId !== null
      ? {
          basePath: '/api/v1/purchase' as const,
          side: 'payable' as const,
          partnerKind: VENDOR_PARTNER_KIND,
          partnerId: vendor.id,
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
    if (vendor === null) {
      setError(t('purchase.form.error.vendorRequired'))
      return
    }
    if (postingDate.trim() === '') {
      setError(t('purchase.form.error.postingDateRequired'))
      return
    }
    if (operationCode === '') {
      setError(t('purchase.form.error.operationRequired'))
      return
    }
    if (payableAccount === null) {
      setError(t('purchase.form.error.payableAccountRequired'))
      return
    }
    if (branchId === null) {
      setError(t('purchase.form.branchMissing'))
      return
    }
    void submitResolved(vendor.id, payableAccount.id, branchId, acknowledgeWarnings)
  }

  // Tách async vì lượt rà mã có thể phải hỏi server (mã ngoài trang seed):
  // tra bù `search=` rồi rà lại — mã sai thật thì lượt hai báo đúng lỗi cũ.
  async function submitResolved(
    vendorId: number,
    payableAccountId: number,
    branch: number,
    acknowledgeWarnings: boolean,
  ): Promise<void> {
    let options = dimensionLookups.options
    let unitOptions = unitLookup.options
    let resolvedLines = resolvePurchaseLines(rows, accountLookup.maps, options, unitOptions, manualLandedCost, t)
    let resolvedCosts = resolveLandedCosts(costRows, accountLookup.maps, options['partners'], t)
    const missing = [...resolvedLines.missing, ...resolvedCosts.missing]
    if (missing.length > 0) {
      const unitCodes = missing.filter((code) => code.slug === 'units_of_measure').map((code) => code.code)
      const catalogCodes = missing.filter((code) => code.slug !== 'units_of_measure')
      if (catalogCodes.length > 0) {
        options = await dimensionLookups.resolveMissingCodes(catalogCodes)
      }
      if (unitCodes.length > 0) {
        const fetched = await unitLookup.fetchCodes(unitCodes)
        const byId = new Map(unitOptions.map((option) => [option.id, option] as const))
        for (const option of fetched) {
          byId.set(option.id, option)
        }
        unitOptions = [...byId.values()]
      }
      resolvedLines = resolvePurchaseLines(rows, accountLookup.maps, options, unitOptions, manualLandedCost, t)
      resolvedCosts = resolveLandedCosts(costRows, accountLookup.maps, options['partners'], t)
    }
    const errors = [...resolvedLines.errors, ...resolvedCosts.errors]
    if (errors.length > 0) {
      setError(errors.join(' '))
      return
    }
    if (resolvedLines.lines.length === 0) {
      setError(t('purchase.form.linesRequired'))
      return
    }

    const received = vendorInvoiceStatus === VENDOR_INVOICE_RECEIVED
    const body: Record<string, unknown> = {
      kind,
      operation_code: operationCode,
      vendor_id: vendorId,
      payable_account_id: payableAccountId,
      buyer_id: buyer?.id ?? (buyerTouched ? null : (voucher?.buyer_id ?? null)),
      branch_id: branch,
      document_date: documentDate.trim() === '' ? postingDate : documentDate,
      posting_date: postingDate,
      currency_code: currencyCode.trim() === '' ? 'VND' : currencyCode.trim(),
      exchange_rate: exchangeRate.trim() === '' ? '1' : exchangeRate.trim(),
      vendor_invoice_status: vendorInvoiceStatus,
      // Chưa có / không có hóa đơn thì ba mảnh số hóa đơn không có nghĩa —
      // không gửi giá trị người dùng từng gõ rồi đổi ý.
      vendor_invoice_form: received && vendorInvoiceForm.trim() !== '' ? vendorInvoiceForm.trim() : null,
      vendor_invoice_serial:
        received && vendorInvoiceSerial.trim() !== '' ? vendorInvoiceSerial.trim() : null,
      vendor_invoice_no: received && vendorInvoiceNo.trim() !== '' ? vendorInvoiceNo.trim() : null,
      vendor_invoice_date: received && vendorInvoiceDate.trim() !== '' ? vendorInvoiceDate : null,
      payment_term_id: paymentTermId === '' ? null : Number.parseInt(paymentTermId, 10),
      due_date: dueDate.trim() === '' ? null : dueDate,
      landed_cost_allocation: allocation,
      description: description.trim() === '' ? null : description.trim(),
      lines: resolvedLines.lines,
      landed_costs: resolvedCosts.costs,
      settlements: isReturn ? buildSettlements() : [],
    }

    if (voucher === null) {
      createMutation.mutate(
        { body: body as unknown as PurchaseInvoiceIn, idempotencyKey, acknowledgeWarnings },
        {
          onSuccess: goToList,
          onError: (caught) => {
            fail(caught, 'create')
          },
        },
      )
      return
    }
    updateMutation.mutate(
      { ...body, row_version: voucher.row_version } as unknown as PurchaseInvoiceUpdate,
      {
        onSuccess: goToList,
        onError: (caught) => {
          fail(caught)
        },
      },
    )
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
      currencyCode !== 'VND' ||
      exchangeRate !== '1' ||
      vendorInvoiceStatus !== VENDOR_INVOICE_RECEIVED ||
      vendorInvoiceNo !== '' ||
      paymentTermId !== '' ||
      dueDate !== '' ||
      buyer !== null ||
      description !== '')

  const settledCount = Object.values(settlementAmounts).filter((amount) => amount.trim() !== '').length

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

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <PurchaseHeaderFields
            vendor={vendor}
            vendorOptions={vendorLookup.options}
            onVendorChange={(option) => {
              setVendor(option)
              // Đổi NCC là đổi danh sách hóa đơn gốc — số đối trừ của NCC cũ
              // không được sống ngầm rồi gửi lên (review 6F-1 H-1).
              setSettlementAmounts({})
            }}
            onVendorQueryChange={vendorLookup.searchFor}
            vendorHint={vendorHint}
            postingDate={postingDate}
            onPostingDateChange={(value) => {
              setPostingDate(value)
              if (!documentDateTouched) {
                setDocumentDate(value)
              }
            }}
            operationCode={operationCode}
            operations={applicableOperations}
            onOperationChange={handleOperationChange}
            vendorInvoiceStatus={vendorInvoiceStatus}
            onVendorInvoiceStatusChange={setVendorInvoiceStatus}
            vendorInvoiceForm={vendorInvoiceForm}
            onVendorInvoiceFormChange={setVendorInvoiceForm}
            vendorInvoiceSerial={vendorInvoiceSerial}
            onVendorInvoiceSerialChange={setVendorInvoiceSerial}
            vendorInvoiceNo={vendorInvoiceNo}
            onVendorInvoiceNoChange={setVendorInvoiceNo}
            vendorInvoiceDate={vendorInvoiceDate}
            onVendorInvoiceDateChange={setVendorInvoiceDate}
            payableAccount={payableAccount}
            payableAccountOptions={payableAccountOptions}
            onPayableAccountChange={setPayableAccount}
            paymentTermId={paymentTermId}
            paymentTermOptions={paymentTerms.options}
            onPaymentTermChange={setPaymentTermId}
            dueDate={dueDate}
            onDueDateChange={setDueDate}
            buyer={buyer}
            buyerOptions={buyerLookup.options}
            onBuyerChange={(option) => {
              setBuyer(option)
              setBuyerTouched(true)
            }}
            onBuyerQueryChange={buyerLookup.searchFor}
            documentDate={documentDate}
            onDocumentDateChange={(value) => {
              setDocumentDate(value)
              setDocumentDateTouched(true)
            }}
            currencyCode={currencyCode}
            onCurrencyCodeChange={setCurrencyCode}
            exchangeRate={exchangeRate}
            onExchangeRateChange={setExchangeRate}
            description={description}
            onDescriptionChange={setDescription}
            defaultAdvancedOpen={hasAdvancedValues}
          />

          <FormCard title={t('purchase.form.card.what')} aside={t('purchase.form.card.whatHint')}>
            <DataGrid
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              caption={t('purchase.line.caption')}
              cellLabel={(header, rowNumber) =>
                t('purchase.line.cellLabel', { header, row: String(rowNumber) })
              }
              onCommit={handleCommit}
              height={260}
            />
            {!isReturn && (
              <LandedCostSection
                rows={costRows}
                onCommit={handleCostCommit}
                allocation={allocation}
                onAllocationChange={setAllocation}
                accounts={accountLookup.maps}
                vendorOptions={dimensionLookups.options['partners'] ?? []}
              />
            )}
          </FormCard>

          {isReturn && (
            <FormCard title={t('purchase.form.card.settlement')}>
              <div className="p-3.5">
                {settlementQuery === null ? (
                  <p className="text-sm text-text-muted">{t('purchase.form.settlementPickVendor')}</p>
                ) : (
                  <SettlementSection
                    query={settlementQuery}
                    amounts={settlementAmounts}
                    onAmountChange={(key, value) => {
                      setSettlementAmounts((current) => ({ ...current, [key]: value }))
                    }}
                    disabled={readOnly || busy}
                  />
                )}
              </div>
            </FormCard>
          )}
        </div>

        <PurchaseSummaryCards
          voucher={voucher}
          kind={kind}
          dueDate={dueDate}
          currencyCode={currencyCode}
          settlementTotal={t('purchase.summary.settlementCount', { count: String(settledCount) })}
        />
      </div>

      <JournalVoucherActionsFooter
        voucherStatus={voucher?.status ?? null}
        voucherId={voucher?.id ?? null}
        documentType={DOCUMENT_TYPE}
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
            {
              onSuccess: goToList,
              onError: (caught) => {
                fail(caught)
              },
            },
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
