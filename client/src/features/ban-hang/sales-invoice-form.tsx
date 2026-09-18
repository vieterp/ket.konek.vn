/**
 * Form hóa đơn bán hàng (bộ xương `#mua-form` dùng lại, U2) —
 * `/ban-hang/chung-tu/moi?kind=0..6` và `/ban-hang/chung-tu/:id`.
 *
 * Ba trường bắt buộc hiện sẵn (Khách hàng · Ngày · Nghiệp vụ), lưới "Bán cái
 * gì" với cụm giá tự điền từ bộ định giá (`use-price-quote.ts`, luật 6F-1:
 * không ghi đè giá người dùng đã gõ), "Mở rộng" thu gọn, cột phải ba thẻ tóm
 * tắt. Ba loại giảm trừ (`SALES_REVERSING_KINDS`) thêm khối đối trừ hóa đơn
 * gốc — cùng `SettlementSection` với phiếu thu, đọc `/sales/open-invoices`.
 * Hai loại điều chỉnh (5/6) chỉ tới qua deep-link
 * `?kind=&adjusts_voucher_id=` từ luồng hỏi–đáp sai sót (7H-3) — thẻ "Điều
 * chỉnh chứng từ" chỉ đọc, không picker (quyết định user 2026-09-18).
 *
 * Cùng khung xương `mua-hang/purchase-invoice-form.tsx`: định tuyến theo `:id`,
 * form dựng từ dữ liệu ĐÃ tải (`key={voucher.id}`), lưới hydrate đúng một lần
 * khi các lượt tra danh mục xong, cảnh báo FR-SYS-062 (ngưỡng nợ khách của 7B)
 * quay về dạng 422 toàn-cảnh-báo → băng "Vẫn ghi sổ?". Lượt xóa vướng tờ HĐĐT
 * nháp (nợ 7D) nói ra bước phải làm qua `delete-blocked-message.ts`.
 */

import type { ReactElement, ReactNode } from 'react'
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import type { DataGridChange, LookupOption } from '@/design-system/components'
import { Alert, DataGrid } from '@/design-system/components'
import { useAccess } from '@/lib/access'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n, type Translate } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useMasterSearchLookup } from '@/features/danh-muc-thiet-lap/use-master-search-lookup'
import { usePartnerOverview } from '@/features/danh-muc-thiet-lap/use-partner'
import { FormCard } from '@/features/mua-hang/form-card'
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

import { deleteBlockedByDraftEInvoice, type DeleteBlockedMessage } from './delete-blocked-message'
import { FeatureNav } from './feature-nav'
import { SalesHeaderFields } from './sales-header-fields'
import { buildSalesLineColumns } from './sales-line-columns'
import { buildRowFromSalesLine } from './sales-line-hydrate'
import { SALES_DIMENSION_COLUMNS, resolveSalesLines } from './sales-line-resolve'
import { applySalesLineChanges, emptySalesLineRow, type SalesLineRow } from './sales-line-types'
import {
  SALES_ADJUSTMENT_KINDS,
  SALES_KIND_GOODS,
  SALES_KIND_LABEL_KEYS,
  SALES_REVERSING_KINDS,
} from './sales-row-status'
import { SalesSummaryCards } from './sales-summary-cards'
import {
  applyQuoteToRow,
  quoteFingerprint,
  quoteRequestFor,
  usePriceQuote,
  type QuoteContext,
} from './use-price-quote'
import {
  useCreateSalesInvoice,
  useSalesInvoice,
  useUpdateSalesInvoice,
  type SalesInvoiceIn,
  type SalesInvoiceOut,
  type SalesInvoiceUpdate,
} from './use-sales-invoices'

const DOCUMENT_TYPE = 'SAL'
/** Khớp `PartnerKind.CUSTOMER` phía server. */
const CUSTOMER_PARTNER_KIND = 0
const DIMENSION_KEYS = SALES_DIMENSION_COLUMNS.map((column) => column.key)

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
  return parsed in SALES_KIND_LABEL_KEYS ? parsed : SALES_KIND_GOODS
}

function kindLabelOf(t: Translate, kind: number): string {
  return t(SALES_KIND_LABEL_KEYS[kind as keyof typeof SALES_KIND_LABEL_KEYS])
}

export function SalesInvoiceForm(): ReactElement {
  const { id } = useParams<{ id?: string }>()
  return id === undefined ? <NewInvoicePage /> : <ExistingInvoicePage id={id} />
}

function NewInvoicePage(): ReactElement {
  const { t } = useI18n()
  const [searchParams] = useSearchParams()
  const kind = parseKind(searchParams.get('kind'))
  const adjustsVoucherId = searchParams.get('adjusts_voucher_id')
  return (
    <FormShell title={t('sales.form.titleCreate', { kind: kindLabelOf(t, kind) })}>
      <InvoiceFormBody
        key={`new-${String(kind)}-${adjustsVoucherId ?? ''}`}
        voucher={null}
        kind={kind}
        adjustsVoucherId={adjustsVoucherId === null || adjustsVoucherId.trim() === '' ? null : adjustsVoucherId}
      />
    </FormShell>
  )
}

function ExistingInvoicePage({ id }: { readonly id: string }): ReactElement {
  const { t } = useI18n()
  const query = useSalesInvoice(id)
  if (query.isPending) {
    return (
      <FormShell title={t('sales.form.title')}>
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
      <FormShell title={t('sales.form.title')}>
        <Alert tone="error">{message}</Alert>
      </FormShell>
    )
  }
  return (
    <FormShell title={t('sales.form.titleEdit', { no: query.data.voucher_no })}>
      <InvoiceFormBody
        key={query.data.id}
        voucher={query.data}
        kind={query.data.kind}
        adjustsVoucherId={query.data.adjusts_voucher_id ?? null}
      />
    </FormShell>
  )
}

function optionOf(id: number, code: string, name: string): LookupOption {
  return { id, code, label: name }
}

function findByCode(options: readonly LookupOption[] | undefined, code: string): LookupOption | undefined {
  const wanted = code.trim().toLowerCase()
  return wanted === '' ? undefined : options?.find((option) => option.code.toLowerCase() === wanted)
}

function InvoiceFormBody({
  voucher,
  kind,
  adjustsVoucherId,
}: {
  readonly voucher: SalesInvoiceOut | null
  readonly kind: number
  /** Chứng từ được điều chỉnh (loại 5/6) — từ URL khi tạo, từ thân khi mở. */
  readonly adjustsVoucherId: string | null
}): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const { readOnly, datasetCode } = useSession()
  const access = useAccess()
  const queryClient = useQueryClient()
  const isReversing = SALES_REVERSING_KINDS.includes(kind)
  const isAdjustment = SALES_ADJUSTMENT_KINDS.includes(kind)

  const [postingDate, setPostingDate] = useState(() => voucher?.posting_date ?? todayIso())
  const [documentDate, setDocumentDate] = useState(
    () => voucher?.document_date ?? voucher?.posting_date ?? todayIso(),
  )
  const [documentDateTouched, setDocumentDateTouched] = useState(voucher !== null)
  const [operationCode, setOperationCode] = useState(() => voucher?.operation_code ?? '')
  const [customer, setCustomer] = useState<LookupOption | null>(null)
  const [invoiceForm, setInvoiceForm] = useState(() => voucher?.invoice_form ?? '')
  const [invoiceSerial, setInvoiceSerial] = useState(() => voucher?.invoice_serial ?? '')
  const [invoiceNo, setInvoiceNo] = useState(() => voucher?.invoice_no ?? '')
  const [invoiceDate, setInvoiceDate] = useState(() => voucher?.invoice_date ?? '')
  const [receivableAccount, setReceivableAccount] = useState<LookupOption | null>(null)
  const [paymentTermId, setPaymentTermId] = useState(() =>
    voucher?.payment_term_id === null || voucher?.payment_term_id === undefined
      ? ''
      : String(voucher.payment_term_id),
  )
  const [dueDate, setDueDate] = useState(() => voucher?.due_date ?? '')
  const [priceList, setPriceList] = useState<LookupOption | null>(null)
  const [salesperson, setSalesperson] = useState<LookupOption | null>(null)
  // NV bán hàng đã lưu mà danh mục không tra được (đã nghỉ) vẫn phải vọng lại
  // y nguyên khi PUT — chỉ khi người dùng chủ động đổi mới gửi giá trị mới
  // (cùng 7H-1 M-5).
  const [salespersonTouched, setSalespersonTouched] = useState(false)
  const [recipientName, setRecipientName] = useState(() => voucher?.recipient_name ?? '')
  const [shipTo, setShipTo] = useState(() => voucher?.ship_to ?? '')
  const [isStockIssue, setIsStockIssue] = useState(() => voucher?.is_stock_issue ?? false)
  const [currencyCode, setCurrencyCode] = useState(() => voucher?.currency_code ?? 'VND')
  const [exchangeRate, setExchangeRate] = useState(() => voucher?.exchange_rate ?? '1')
  const [description, setDescription] = useState(() => voucher?.description ?? '')
  const [rows, setRows] = useState<SalesLineRow[]>(() => [emptySalesLineRow()])
  const [settlementAmounts, setSettlementAmounts] = useState<Readonly<Record<string, string>>>({})
  const [error, setError] = useState<string | null>(null)
  // Lỗi hỏi giá đi băng riêng, không qua `fail()`: nó không phải lỗi của
  // chứng từ (quyền `master.price_lists.view` có thể thiếu ở người bán hàng),
  // và không được xóa băng "Vẫn ghi sổ?" đang chờ (review 7H-2a M-2).
  const [quoteError, setQuoteError] = useState<string | null>(null)
  const [deleteBlocked, setDeleteBlocked] = useState<DeleteBlockedMessage | null>(null)
  const [violations, setViolations] = useState<readonly Violation[]>([])
  const [failedIntent, setFailedIntent] = useState<'create' | 'post' | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [idempotencyKey] = useState(newIdempotencyKey)

  const operations = useAutoPostingOperations(DOCUMENT_TYPE, postingDate)
  const operationItems = operations.data?.items ?? []
  const [operationDefaulted, setOperationDefaulted] = useState(voucher !== null)

  const customerLookup = useMasterSearchLookup('partners', voucher === null ? [] : [voucher.customer_id])
  const salespersonLookup = useMasterSearchLookup(
    'employees',
    voucher?.salesperson_id === null || voucher?.salesperson_id === undefined
      ? []
      : [voucher.salesperson_id],
  )
  const paymentTerms = useMasterSearchLookup('payment_terms')
  const priceListLookup = useMasterSearchLookup(
    'price_lists',
    voucher?.price_list_id === null || voucher?.price_list_id === undefined ? [] : [voucher.price_list_id],
  )
  const unitLookup = useMasterSearchLookup(
    'units_of_measure',
    voucher?.lines.flatMap((line) => (line.unit_id === null ? [] : [line.unit_id])) ?? [],
  )
  // Thẻ công nợ của khách (BFF 7G-4) chỉ để lấy MST + điều khoản cho dòng gợi ý
  // dưới ô khách; không có quyền danh mục thì ô vẫn dùng được, chỉ thiếu gợi ý.
  const customerOverview = usePartnerOverview(customer?.id ?? null)
  // Chứng từ được điều chỉnh — chỉ để hiện số chứng từ trên thẻ chỉ đọc.
  const adjusted = useSalesInvoice(adjustsVoucherId)
  const priceQuote = usePriceQuote()
  // Thế hệ ngữ cảnh hỏi giá: đổi khách/ngày/bảng giá là một thế hệ mới, kết
  // quả của thế hệ cũ về muộn thì bỏ (review 7H-2a H-1).
  const quoteGeneration = useRef(0)

  const requiredAccountIds = [
    ...(voucher?.lines.flatMap((line) =>
      [line.account_id, line.vat_account_id].filter((value): value is number => value !== null),
    ) ?? []),
    ...(voucher === null ? [] : [voucher.receivable_account_id]),
  ]
  const accountLookup = useAccountLookup(postingDate, requiredAccountIds)
  // Mặt hàng/kho/chiều trên dòng: id trên chứng từ cũ tra bù trước khi dựng
  // lưới (nợ M-B 6F-1).
  const dimensionLookups = useDimensionLookups(requiredDimensionIdsOf(voucher?.lines ?? []))
  const [hydrated, setHydrated] = useState(false)

  if (
    voucher !== null &&
    !hydrated &&
    !accountLookup.isLoading &&
    !dimensionLookups.isLoading &&
    !customerLookup.isLoading &&
    !salespersonLookup.isLoading &&
    !priceListLookup.isLoading &&
    !unitLookup.isLoading
  ) {
    setHydrated(true)
    setRows(
      voucher.lines.length === 0
        ? [emptySalesLineRow()]
        : voucher.lines.map((line) =>
            buildRowFromSalesLine(line, accountLookup.maps, dimensionLookups.options, unitLookup.options),
          ),
    )
    const receivable = accountLookup.maps.byId.get(voucher.receivable_account_id)
    if (receivable !== undefined) {
      setReceivableAccount(optionOf(receivable.id, receivable.code, receivable.name))
    }
    setCustomer(customerLookup.byId.get(voucher.customer_id) ?? null)
    if (voucher.salesperson_id !== null && voucher.salesperson_id !== undefined) {
      setSalesperson(salespersonLookup.byId.get(voucher.salesperson_id) ?? null)
    }
    if (voucher.price_list_id !== null && voucher.price_list_id !== undefined) {
      setPriceList(priceListLookup.byId.get(voucher.price_list_id) ?? null)
    }
    if (voucher.settlements.length > 0) {
      setSettlementAmounts(
        Object.fromEntries(
          voucher.settlements.map((row) => [settlementKey(row.target_kind, row.target_id), row.amount_fc]),
        ),
      )
    }
  }

  const createMutation = useCreateSalesInvoice()
  const updateMutation = useUpdateSalesInvoice(voucher?.id ?? '')
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
        void queryClient.invalidateQueries({ queryKey: ['sales', datasetCode, 'invoice', voucher.id] })
      }
    } else {
      setError(t('error.transport.unreachable'))
      setViolations([])
    }
  }

  function goToList(): void {
    void navigate('/ban-hang/chung-tu')
  }

  function quoteContext(overrides: Partial<QuoteContext> = {}): QuoteContext {
    return {
      onDate: postingDate,
      customerId: customer?.id ?? null,
      priceListId: priceList?.id ?? null,
      ...overrides,
    }
  }

  /**
   * Hỏi giá cho những dòng nêu tên (chưa chốt giá tay, có mã hàng) rồi chép
   * kết quả lên đúng dòng theo `id` — lưới có thể đã đổi trong lúc chờ server,
   * nên không ghi theo chỉ số. Mã hàng ngoài trang seed được tra bù trước.
   */
  async function requote(
    snapshot: readonly SalesLineRow[],
    rowIds: readonly string[],
    context: QuoteContext,
    generation: number,
  ): Promise<void> {
    const targets = snapshot.filter(
      (row) => rowIds.includes(row.id) && !row.priceTyped && row.itemCode.trim() !== '',
    )
    if (targets.length === 0) {
      return
    }
    let items = dimensionLookups.options['items']
    const missing = targets
      .filter((row) => findByCode(items, row.itemCode) === undefined)
      .map((row) => ({ slug: 'items', code: row.itemCode.trim() }))
    try {
      if (missing.length > 0) {
        items = (await dimensionLookups.resolveMissingCodes(missing))['items']
      }
      const asked = targets.flatMap((row) => {
        const request = quoteRequestFor(row, context, items, unitLookup.options)
        return request === null ? [] : [{ rowId: row.id, fingerprint: quoteFingerprint(row), request }]
      })
      if (asked.length === 0) {
        return
      }
      const quotes = await priceQuote.quoteBatch(asked.map((entry) => entry.request))
      if (generation !== quoteGeneration.current) {
        // Khách/ngày/bảng giá đã đổi trong lúc chờ — thế hệ mới đã hỏi lại.
        return
      }
      setQuoteError(null)
      setRows((current) =>
        current.map((row) => {
          const index = asked.findIndex((entry) => entry.rowId === row.id)
          const entry = index < 0 ? undefined : asked[index]
          const quote = index < 0 ? undefined : quotes[index]
          // Chỉ chép khi dòng vẫn đang hỏi ĐÚNG câu này — mã hàng/ĐVT/SL/thuế
          // đổi trong lúc chờ thì đáp án cũ là của câu cũ.
          return quote === undefined || entry === undefined || quoteFingerprint(row) !== entry.fingerprint
            ? row
            : applyQuoteToRow(row, quote)
        }),
      )
    } catch (caught) {
      setQuoteError(
        caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
      )
    }
  }

  /** Đổi khách / ngày / bảng giá là đổi câu hỏi giá của MỌI dòng chưa chốt tay. */
  function requoteAll(overrides: Partial<QuoteContext>): void {
    quoteGeneration.current += 1
    void requote(
      rows,
      rows.map((row) => row.id),
      quoteContext(overrides),
      quoteGeneration.current,
    )
  }

  function handleCommit(changes: readonly DataGridChange[]): void {
    const result = applySalesLineChanges(rows, changes, DIMENSION_KEYS)
    setRows(result.rows)
    for (const change of changes) {
      if (change.columnKey === 'account' || change.columnKey === 'vat_account') {
        accountLookup.resolve(change.value)
      }
    }
    if (result.requote.length > 0) {
      void requote(
        result.rows,
        result.requote.flatMap((index) => {
          const row = result.rows[index]
          return row === undefined ? [] : [row.id]
        }),
        quoteContext(),
        quoteGeneration.current,
      )
    }
  }

  /** Hai bên TK của một nghiệp vụ theo LOẠI chứng từ: hóa đơn thường Nợ TK phải thu; giảm trừ đảo chiều — bên phải thu là bên Có. */
  function operationSides(operation: AutoPostingOperation): {
    readonly receivableCode: string | null
    readonly lineCode: string | null
  } {
    return isReversing
      ? { receivableCode: operation.credit_account_code, lineCode: operation.debit_account_code }
      : { receivableCode: operation.debit_account_code, lineCode: operation.credit_account_code }
  }

  /**
   * Nghiệp vụ hợp với loại chứng từ (7H-1 M-3 đảo chiều): bên phải thu của nó
   * phải là TK theo dõi khách hàng. Chưa tải hệ thống TK thì chưa lọc.
   */
  const applicableOperations = operationItems.filter((operation) => {
    // Nghiệp vụ ĐÃ LƯU trên chứng từ luôn có trong danh sách (6F-1 C-1).
    if (operation.operation_code === operationCode) {
      return true
    }
    const { receivableCode } = operationSides(operation)
    if (receivableCode === null) {
      return false
    }
    const account = accountLookup.maps.byCode.get(receivableCode.toLowerCase())
    return account === undefined || (account.detail_tracking ?? []).includes('customer')
  })

  /** Điền sẵn TK phải thu + TK dòng đầu khi chúng còn TRỐNG (luật 6F-1). */
  function prefillFromOperation(operation: AutoPostingOperation): void {
    const { receivableCode, lineCode } = operationSides(operation)
    if (receivableCode !== null) {
      const account = accountLookup.maps.byCode.get(receivableCode.toLowerCase())
      if (receivableAccount === null && account !== undefined) {
        setReceivableAccount(optionOf(account.id, account.code, account.name))
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

  // Form MỚI mà loại chứng từ chỉ có đúng MỘT nghiệp vụ hợp thì chọn sẵn —
  // cùng lối "điều chỉnh state trong thân render một lần".
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
    const { receivableCode, lineCode } = operationSides(operation)
    if (receivableCode !== null) {
      accountLookup.resolve(receivableCode)
    }
    if (lineCode !== null) {
      accountLookup.resolve(lineCode)
    }
    prefillFromOperation(operation)
  }

  const receivableAccountOptions: LookupOption[] = [...accountLookup.maps.byCode.values()]
    .filter((account) => !account.is_summary && (account.detail_tracking ?? []).includes('customer'))
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
  const visibleDimensionColumns = SALES_DIMENSION_COLUMNS.filter((column) =>
    column.values.some((value) => requiredDimensions.has(value)),
  )
  const columns = buildSalesLineColumns({
    t,
    accounts: accountLookup.maps,
    items: dimensionLookups.options['items'] ?? [],
    visibleDimensionColumns,
  })

  const branchId = voucher !== null ? voucher.branch_id : (access.data?.acting_branch_id ?? null)

  const partnerInfo = customerOverview.data?.partner
  const customerHint =
    customer === null || partnerInfo === undefined
      ? null
      : [
          partnerInfo.tax_code ? t('sales.form.customerHint.taxCode', { code: partnerInfo.tax_code }) : null,
          partnerInfo.payment_term_id === null || partnerInfo.payment_term_id === undefined
            ? null
            : t('sales.form.customerHint.term', {
                term:
                  paymentTerms.byId.get(partnerInfo.payment_term_id)?.label ??
                  String(partnerInfo.payment_term_id),
              }),
        ]
          .filter((part): part is string => part !== null)
          .join(' · ') || null

  const settlementQuery =
    isReversing && customer !== null && branchId !== null
      ? {
          basePath: '/api/v1/sales' as const,
          side: 'receivable' as const,
          partnerKind: CUSTOMER_PARTNER_KIND,
          partnerId: customer.id,
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
    setDeleteBlocked(null)
    setViolations([])
    setFailedIntent(null)
    if (isAdjustment && adjustsVoucherId === null) {
      setError(t('sales.form.error.adjustsRequired'))
      return
    }
    if (customer === null) {
      setError(t('sales.form.error.customerRequired'))
      return
    }
    if (postingDate.trim() === '') {
      setError(t('sales.form.error.postingDateRequired'))
      return
    }
    if (operationCode === '') {
      setError(t('sales.form.error.operationRequired'))
      return
    }
    if (receivableAccount === null) {
      setError(t('sales.form.error.receivableAccountRequired'))
      return
    }
    if (branchId === null) {
      setError(t('sales.form.branchMissing'))
      return
    }
    void submitResolved(customer.id, receivableAccount.id, branchId, acknowledgeWarnings)
  }

  // Tách async vì lượt rà mã có thể phải hỏi server (mã ngoài trang seed):
  // tra bù `search=` rồi rà lại — mã sai thật thì lượt hai báo đúng lỗi cũ.
  async function submitResolved(
    customerId: number,
    receivableAccountId: number,
    branch: number,
    acknowledgeWarnings: boolean,
  ): Promise<void> {
    let options = dimensionLookups.options
    let unitOptions = unitLookup.options
    let resolved = resolveSalesLines(rows, accountLookup.maps, options, unitOptions, t)
    if (resolved.missing.length > 0) {
      const unitCodes = resolved.missing
        .filter((code) => code.slug === 'units_of_measure')
        .map((code) => code.code)
      const catalogCodes = resolved.missing.filter((code) => code.slug !== 'units_of_measure')
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
      resolved = resolveSalesLines(rows, accountLookup.maps, options, unitOptions, t)
    }
    if (resolved.errors.length > 0) {
      setError(resolved.errors.join(' '))
      return
    }
    if (resolved.lines.length === 0) {
      setError(t('sales.form.linesRequired'))
      return
    }

    const body: Record<string, unknown> = {
      kind,
      adjusts_voucher_id: isAdjustment ? adjustsVoucherId : null,
      operation_code: operationCode,
      customer_id: customerId,
      receivable_account_id: receivableAccountId,
      salesperson_id:
        salesperson?.id ?? (salespersonTouched ? null : (voucher?.salesperson_id ?? null)),
      branch_id: branch,
      document_date: documentDate.trim() === '' ? postingDate : documentDate,
      posting_date: postingDate,
      currency_code: currencyCode.trim() === '' ? 'VND' : currencyCode.trim(),
      exchange_rate: exchangeRate.trim() === '' ? '1' : exchangeRate.trim(),
      ship_to: shipTo.trim() === '' ? null : shipTo.trim(),
      recipient_name: recipientName.trim() === '' ? null : recipientName.trim(),
      invoice_form: invoiceForm.trim() === '' ? null : invoiceForm.trim(),
      invoice_serial: invoiceSerial.trim() === '' ? null : invoiceSerial.trim(),
      invoice_no: invoiceNo.trim() === '' ? null : invoiceNo.trim(),
      invoice_date: invoiceDate.trim() === '' ? null : invoiceDate,
      payment_term_id: paymentTermId === '' ? null : Number.parseInt(paymentTermId, 10),
      due_date: dueDate.trim() === '' ? null : dueDate,
      price_list_id: priceList?.id ?? null,
      is_stock_issue: isStockIssue,
      description: description.trim() === '' ? null : description.trim(),
      lines: resolved.lines,
      settlements: isReversing ? buildSettlements() : [],
    }

    if (voucher === null) {
      createMutation.mutate(
        { body: body as unknown as SalesInvoiceIn, idempotencyKey, acknowledgeWarnings },
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
      { ...body, row_version: voucher.row_version } as unknown as SalesInvoiceUpdate,
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
      invoiceNo !== '' ||
      paymentTermId !== '' ||
      dueDate !== '' ||
      priceList !== null ||
      salesperson !== null ||
      recipientName !== '' ||
      shipTo !== '' ||
      isStockIssue ||
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
      {quoteError !== null && (
        <Alert tone="warning">{t('sales.form.quoteFailed', { detail: quoteError })}</Alert>
      )}
      {deleteBlocked !== null && (
        <Alert tone="error">
          {deleteBlocked.text}{' '}
          <Link to={deleteBlocked.href} className="font-semibold underline">
            {deleteBlocked.linkLabel}
          </Link>
        </Alert>
      )}
      {isAdjustment && adjustsVoucherId === null && (
        <Alert tone="warning">{t('sales.form.adjustsMissing')}</Alert>
      )}

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          {isAdjustment && adjustsVoucherId !== null && (
            <FormCard title={t('sales.form.card.adjusts')}>
              <p className="p-3.5 text-sm text-text-default">
                {adjusted.data === undefined
                  ? t('sales.form.adjustsVoucher', { no: adjustsVoucherId })
                  : t('sales.form.adjustsVoucher', { no: adjusted.data.voucher_no })}
                <Link to={`/ban-hang/chung-tu/${adjustsVoucherId}`} className="ml-2 text-secondary hover:underline">
                  {t('sales.form.adjustsOpen')}
                </Link>
              </p>
            </FormCard>
          )}

          <SalesHeaderFields
            customer={customer}
            customerOptions={customerLookup.options}
            onCustomerChange={(option) => {
              setCustomer(option)
              // Đổi khách là đổi danh sách hóa đơn gốc — số đối trừ của khách
              // cũ không được sống ngầm rồi gửi lên (review 6F-1 H-1).
              setSettlementAmounts({})
              requoteAll({ customerId: option?.id ?? null })
            }}
            onCustomerQueryChange={customerLookup.searchFor}
            customerHint={customerHint}
            postingDate={postingDate}
            onPostingDateChange={(value) => {
              setPostingDate(value)
              if (!documentDateTouched) {
                setDocumentDate(value)
              }
              requoteAll({ onDate: value })
            }}
            operationCode={operationCode}
            operations={applicableOperations}
            onOperationChange={handleOperationChange}
            invoiceForm={invoiceForm}
            onInvoiceFormChange={setInvoiceForm}
            invoiceSerial={invoiceSerial}
            onInvoiceSerialChange={setInvoiceSerial}
            invoiceNo={invoiceNo}
            onInvoiceNoChange={setInvoiceNo}
            invoiceDate={invoiceDate}
            onInvoiceDateChange={setInvoiceDate}
            receivableAccount={receivableAccount}
            receivableAccountOptions={receivableAccountOptions}
            onReceivableAccountChange={setReceivableAccount}
            paymentTermId={paymentTermId}
            paymentTermOptions={paymentTerms.options}
            onPaymentTermChange={setPaymentTermId}
            dueDate={dueDate}
            onDueDateChange={setDueDate}
            priceList={priceList}
            priceListOptions={priceListLookup.options}
            onPriceListChange={(option) => {
              setPriceList(option)
              requoteAll({ priceListId: option?.id ?? null })
            }}
            salesperson={salesperson}
            salespersonOptions={salespersonLookup.options}
            onSalespersonChange={(option) => {
              setSalesperson(option)
              setSalespersonTouched(true)
            }}
            onSalespersonQueryChange={salespersonLookup.searchFor}
            recipientName={recipientName}
            onRecipientNameChange={setRecipientName}
            shipTo={shipTo}
            onShipToChange={setShipTo}
            isStockIssue={isStockIssue}
            onIsStockIssueChange={setIsStockIssue}
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

          <FormCard title={t('sales.form.card.what')} aside={t('sales.form.card.whatHint')}>
            <DataGrid
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              caption={t('sales.line.caption')}
              cellLabel={(header, rowNumber) =>
                t('sales.line.cellLabel', { header, row: String(rowNumber) })
              }
              onCommit={handleCommit}
              height={260}
            />
          </FormCard>

          {isReversing && (
            <FormCard title={t('sales.form.card.settlement')}>
              <div className="p-3.5">
                {settlementQuery === null ? (
                  <p className="text-sm text-text-muted">{t('sales.form.settlementPickCustomer')}</p>
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

        <SalesSummaryCards
          voucher={voucher}
          kind={kind}
          dueDate={dueDate}
          currencyCode={currencyCode}
          settlementTotal={t('sales.summary.settlementCount', { count: String(settledCount) })}
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
          setDeleteBlocked(null)
          actions.remove.mutate(voucher.id, {
            onSuccess: goToList,
            onError: (caught) => {
              // Tờ HĐĐT nháp giữ chứng từ lại (khóa ngoại RESTRICT, nợ 7D) —
              // nói ra bước phải làm thay vì câu ràng buộc chung.
              const blocked =
                caught instanceof ApiError
                  ? deleteBlockedByDraftEInvoice(t, caught.problem, voucher.id)
                  : null
              if (blocked !== null) {
                setDeleteBlocked(blocked)
                setConfirmDelete(false)
                return
              }
              fail(caught)
            },
          })
        }}
      />
    </div>
  )
}
