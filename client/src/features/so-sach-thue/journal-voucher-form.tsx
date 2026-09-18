/**
 * Form chứng từ nghiệp vụ khác (U2) — `/so-sach-thue/chung-tu/moi` và
 * `/so-sach-thue/chung-tu/:id`.
 *
 * Ba thứ thiết yếu hiện sẵn: Ngày hạch toán, Diễn giải, lưới chi tiết
 * (`journal-voucher-header-fields.tsx` vẽ hai cái đầu + khối "Mở rộng"). Nút
 * bấm cuối form ở `journal-voucher-actions-footer.tsx`.
 *
 * `JournalVoucherForm` chỉ định tuyến theo `:id` có mặt hay không;
 * `ExistingVoucherPage` chờ chứng từ tải xong rồi mới dựng `VoucherFormBody`
 * — dựng form từ dữ liệu CHƯA có sẽ phải vá bằng effect đồng bộ lại, đúng loại
 * lỗi "state khởi tạo từ dữ liệu tới muộn" mà `CatalogEditDrawer` từng tránh
 * bằng `key={record?.id}`.
 */

import type { ReactElement, ReactNode } from 'react'
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'

import type { DataGridChange } from '@/design-system/components'
import { Alert, DataGrid } from '@/design-system/components'
import { useAccess } from '@/lib/access'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { DIMENSION_COLUMNS } from './dimension-config'
import { FeatureNav } from './feature-nav'
import {
  debtLinesOf,
  debtPartnerKindOf,
  lineNoOf,
  ROW_KEY_SEPARATOR,
  rowSettlementKey,
  SETTLEMENT_CONTEXT_COLUMNS,
} from './journal-debt-lines'
import { JournalVoucherActionsFooter } from './journal-voucher-actions-footer'
import { JournalVoucherHeaderFields } from './journal-voucher-header-fields'
import { buildRowFromLine } from './journal-line-hydrate'
import { buildLineColumns } from './journal-line-columns'
import { resolveLines } from './journal-line-resolve'
import { applyLineChanges, emptyLineRow, isLineRowEmpty, type LineRow } from './journal-line-types'
import { JournalSettlementBlock } from './journal-settlement-block'
import { JournalViolationsAlert } from './journal-violations-alert'
import { extractViolations, type Violation } from './journal-violations'
import { todayIso } from './local-date'
import { useAccountLookup } from './use-account-lookup'
import { requiredDimensionIdsOf, useDimensionLookups } from './use-dimension-lookups'
import {
  useCreateJournalVoucher,
  useJournalVoucher,
  useUpdateJournalVoucher,
  type JournalVoucherIn,
  type JournalVoucherOut,
  type JournalVoucherUpdate,
} from './use-journal-voucher'
import { useVoucherActions } from './use-voucher-actions'

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

export function JournalVoucherForm(): ReactElement {
  const { id } = useParams<{ id?: string }>()
  return id === undefined ? <NewVoucherPage /> : <ExistingVoucherPage id={id} />
}

function NewVoucherPage(): ReactElement {
  const { t } = useI18n()
  return (
    <FormShell title={t('gl.form.titleCreate')}>
      <VoucherFormBody key="new" voucher={null} />
    </FormShell>
  )
}

function ExistingVoucherPage({ id }: { readonly id: string }): ReactElement {
  const { t } = useI18n()
  const query = useJournalVoucher(id)

  if (query.isPending) {
    return (
      <FormShell title={t('gl.voucher.title')}>
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
      <FormShell title={t('gl.voucher.title')}>
        <Alert tone="error">{message}</Alert>
      </FormShell>
    )
  }
  return (
    <FormShell title={t('gl.form.titleEdit', { no: query.data.voucher_no })}>
      <VoucherFormBody key={query.data.id} voucher={query.data} />
    </FormShell>
  )
}

const DIMENSION_KEYS = DIMENSION_COLUMNS.map((column) => column.key)

function VoucherFormBody({
  voucher,
}: {
  readonly voucher: JournalVoucherOut | null
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
  // Chỉ tự đồng bộ "Ngày chứng từ" theo "Ngày hạch toán" khi người dùng CHƯA
  // tự tay sửa nó — sửa chứng từ có sẵn coi như đã "chạm" vì hai ngày có thể
  // khác nhau ngay từ đầu.
  const [documentDateTouched, setDocumentDateTouched] = useState(voucher !== null)
  const [description, setDescription] = useState(() => voucher?.description ?? '')
  const [currencyCode, setCurrencyCode] = useState(() => voucher?.currency_code ?? 'VND')
  const [exchangeRate, setExchangeRate] = useState(() => voucher?.exchange_rate ?? '1')
  const [cashflowActivity, setCashflowActivity] = useState(() =>
    voucher?.cashflow_activity === null || voucher?.cashflow_activity === undefined
      ? ''
      : String(voucher.cashflow_activity),
  )
  const [entryKind, setEntryKind] = useState(() => String(voucher?.entry_kind ?? 0))
  const [rows, setRows] = useState<LineRow[]>(() => [emptyLineRow()])
  // Số đối trừ người dùng gõ, khóa `${rowId}|${target_kind}:${target_id}` —
  // đối trừ của GLE thuộc về DÒNG (7C-3), nên khóa mang id dòng lưới.
  const [settlementAmounts, setSettlementAmounts] = useState<Readonly<Record<string, string>>>({})
  const [error, setError] = useState<string | null>(null)
  const [violations, setViolations] = useState<readonly Violation[]>([])
  // Lệnh vừa bị từ chối — để nút "Vẫn ghi sổ?" (FR-SYS-062) biết gửi lại đúng
  // lệnh đó kèm `acknowledge_warnings=true`. PUT không có trong đây: sửa phiếu
  // không ghi sổ nên không có cảnh báo nghiệp vụ để xác nhận.
  const [failedIntent, setFailedIntent] = useState<'create' | 'post' | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [idempotencyKey] = useState(newIdempotencyKey)

  // `requiredIds` = các TK của chứng từ đang sửa: hệ TK thật vượt trần trang
  // đầu nên hook phải tra bù theo id trước khi form dựng lại dòng (H-1).
  const accountLookup = useAccountLookup(
    postingDate,
    voucher?.lines.map((line) => line.account_id) ?? [],
  )
  // Cùng lý do với `requiredIds` của TK: danh mục chiều tra server-side (nợ
  // M-B 6F-1), bản ghi dòng cũ tham chiếu phải được tra bù trước khi dựng lưới.
  const dimensionLookups = useDimensionLookups(requiredDimensionIdsOf(voucher?.lines ?? []))
  const [hydrated, setHydrated] = useState(false)

  // Dựng lại lưới từ chứng từ đã lưu đúng MỘT lần, khi cả bản đồ TK lẫn danh
  // mục chiều đã tải xong — "điều chỉnh state trong thân render" (khuyến nghị
  // của React) thay vì effect: `accountLookup`/`dimensionLookups` đã là giá
  // trị của LƯỢT RENDER này (không phải thứ đến từ ngoài React), nên tính
  // toán ngay ở đây thay vì đợi thêm một lượt effect. Chạy sớm hơn (khi còn
  // đang tải) thì mã TK/mã chiều sẽ hiện trống trơn. Cờ `hydrated` là STATE,
  // không phải ref: đọc ref trong thân render bị luật `react-hooks/refs` cấm.
  if (voucher !== null && !hydrated && !accountLookup.isLoading && !dimensionLookups.isLoading) {
    setHydrated(true)
    setRows(
      voucher.lines.length === 0
        ? [emptyLineRow()]
        : voucher.lines.map((line) =>
            buildRowFromLine(line, accountLookup.maps, dimensionLookups.options),
          ),
    )
    // `buildRowFromLine` giữ `LineRow.id = JournalLineOut.id`, nên khóa theo
    // `journal_line_id` của dòng đối trừ trỏ đúng dòng lưới vừa dựng.
    if (voucher.settlements.length > 0) {
      setSettlementAmounts(
        Object.fromEntries(
          voucher.settlements.map((row) => [
            rowSettlementKey(row.journal_line_id, row.target_kind, row.target_id),
            row.amount_fc,
          ]),
        ),
      )
    }
  }

  const createMutation = useCreateJournalVoucher()
  const updateMutation = useUpdateJournalVoucher(voucher?.id ?? '')
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
      // 409: người khác vừa lưu — nạp lại chứng từ để lần Cất sau mang
      // `row_version` mới, thay vì 409 mãi tới khi rời trang (review 4E, M-7).
      // Giá trị đang gõ giữ nguyên: state của form đã tách khỏi prop.
      if (caught.status === 409 && voucher !== null) {
        void queryClient.invalidateQueries({
          queryKey: ['gl-journal-voucher', datasetCode, voucher.id],
        })
      }
    } else {
      setError(t('error.transport.unreachable'))
      setViolations([])
    }
  }

  function goToList(): void {
    void navigate('/so-sach-thue/chung-tu')
  }

  function handleCommit(changes: readonly DataGridChange[]): void {
    const next = applyLineChanges(rows, changes, DIMENSION_KEYS)
    setRows(next)
    const partnerCodesToResolve: string[] = []
    // Dòng đổi TK / đối tác / bên thì mọi số đối trừ đã gõ cho dòng ấy thuộc về
    // một ngữ cảnh không còn: đích cũ không hiện trên khối nữa, không có ô để
    // xóa, mà vẫn gửi lên là 422 không lối ra (review 7H-2b H-1). Bỏ khóa của
    // dòng ngay tại đây — cùng khuôn 7H-2a M-5 (đổi mã hàng xóa cụm giá).
    const purgedRowIds = new Set<string>()
    for (const change of changes) {
      if (change.columnKey === 'account') {
        accountLookup.resolve(change.value)
      }
      const row = next[change.rowIndex]
      if (row !== undefined && SETTLEMENT_CONTEXT_COLUMNS.has(change.columnKey)) {
        purgedRowIds.add(row.id)
      }
      if (change.columnKey === 'partner') {
        const typed = change.value.trim().toLowerCase()
        const account = accountLookup.maps.byCode.get((row?.accountCode ?? '').trim().toLowerCase())
        // Chỉ tra khi dòng có thể chạm công nợ: TK chưa tra được (tra sau) hoặc
        // TK theo dõi khách/NCC — TK nhân viên, TK thường không cần lượt gọi.
        const mayTouchDebt = account === undefined || debtPartnerKindOf(account.detail_tracking) !== undefined
        const known = dimensionLookups.options.partners?.some(
          (option) => option.code.toLowerCase() === typed,
        )
        if (typed !== '' && known !== true && mayTouchDebt) {
          partnerCodesToResolve.push(change.value.trim())
        }
      }
    }
    if (purgedRowIds.size > 0) {
      setSettlementAmounts((current) =>
        Object.fromEntries(
          Object.entries(current).filter(
            ([key]) => !purgedRowIds.has(key.slice(0, key.indexOf(ROW_KEY_SEPARATOR))),
          ),
        ),
      )
    }
    // Mã đối tượng ngoài trang seed được tra NGAY lúc gõ (không chờ tới lúc
    // Cất như các chiều khác): khối đối trừ chỉ hiện khi mã tra được, và
    // người dùng cần thấy nó trước khi cất chứ không phải sau.
    if (partnerCodesToResolve.length > 0) {
      void dimensionLookups.resolveMissingCodes(
        partnerCodesToResolve.map((code) => ({ slug: 'partners', code })),
      )
    }
  }

  const debtLines = debtLinesOf(rows, accountLookup.maps, dimensionLookups.options.partners)

  /**
   * Dòng đối trừ gửi lên: chỉ các khóa còn trỏ vào một dòng công nợ ĐANG có
   * trên lưới (dòng bị xóa/đổi TK/đổi đối tác thì khóa của nó thành mồ côi và
   * bị bỏ), `line_no` theo thang server (dòng không trắng, từ 1).
   */
  function buildSettlements(): { line_no: number; target_kind: number; target_id: string; amount_fc: string }[] {
    const liveRowIds = new Set(debtLines.map((line) => line.rowId))
    const built: { line_no: number; target_kind: number; target_id: string; amount_fc: string }[] = []
    for (const [key, value] of Object.entries(settlementAmounts)) {
      if (value.trim() === '') {
        continue
      }
      const separator = key.indexOf(ROW_KEY_SEPARATOR)
      const rowId = key.slice(0, separator)
      if (!liveRowIds.has(rowId)) {
        continue
      }
      const lineNo = lineNoOf(rows, rowId, isLineRowEmpty)
      if (lineNo === null) {
        continue
      }
      const [kind, targetId] = key.slice(separator + 1).split(':')
      if (kind === undefined || targetId === undefined) {
        continue
      }
      built.push({
        line_no: lineNo,
        target_kind: Number.parseInt(kind, 10),
        target_id: targetId,
        amount_fc: value.trim(),
      })
    }
    return built
  }

  const requiredDimensions = new Set<string>()
  for (const row of rows) {
    const account = accountLookup.maps.byCode.get(row.accountCode.trim().toLowerCase())
    for (const value of account?.detail_tracking ?? []) {
      requiredDimensions.add(value)
    }
  }
  const visibleDimensionColumns = DIMENSION_COLUMNS.filter((column) =>
    column.values.some((value) => requiredDimensions.has(value)),
  )
  const columns = buildLineColumns(t, accountLookup.maps, visibleDimensionColumns)

  function handleSave(acknowledgeWarnings = false): void {
    setError(null)
    setViolations([])
    setFailedIntent(null)

    if (postingDate.trim() === '') {
      setError(t('gl.form.error.postingDateRequired'))
      return
    }
    if (description.trim() === '') {
      setError(t('gl.form.error.descriptionRequired'))
      return
    }
    // Chứng từ đã cất KHÔNG đổi được chi nhánh (rào 4A) — lượt sửa phải gửi
    // lại đúng chi nhánh của chứng từ; gửi chi nhánh ĐANG THAO TÁC sẽ 422 với
    // người đa chi nhánh mở chứng từ của chi nhánh khác (review 4E, H-3).
    const branchId = voucher !== null ? voucher.branch_id : (access.data?.acting_branch_id ?? null)
    if (branchId === null) {
      setError(t('gl.form.branchMissing'))
      return
    }

    void submitResolvedLines(branchId, acknowledgeWarnings)
  }

  // Tách async khỏi `handleSave` vì lượt rà mã chiều có thể phải hỏi server
  // (hai-lượt-rà, nợ M-B 6F-1): mã ngoài trang seed được tra `search=` rồi rà
  // lại — mã sai thật thì lượt hai báo đúng lỗi cũ, không có đường lỗi mới.
  async function submitResolvedLines(branchId: number, acknowledgeWarnings: boolean): Promise<void> {
    let resolved = resolveLines(rows, accountLookup.maps, dimensionLookups.options, t)
    if (resolved.missing.length > 0) {
      const mergedOptions = await dimensionLookups.resolveMissingCodes(resolved.missing)
      resolved = resolveLines(rows, accountLookup.maps, mergedOptions, t)
    }
    if (resolved.errors.length > 0) {
      setError(resolved.errors.join(' '))
      return
    }
    if (resolved.lines.length === 0) {
      setError(t('gl.form.linesRequired'))
      return
    }

    const body: Record<string, unknown> = {
      branch_id: branchId,
      document_date: documentDate.trim() === '' ? postingDate : documentDate,
      posting_date: postingDate,
      currency_code: currencyCode.trim() === '' ? 'VND' : currencyCode.trim(),
      exchange_rate: exchangeRate.trim() === '' ? '1' : exchangeRate.trim(),
      description: description.trim(),
      // LUÔN gửi: PUT thay trọn bộ thân chứng từ, nên bỏ trường này là âm thầm
      // đặt lại cờ về "nghiệp vụ" mỗi lần người dùng sửa (review 4F, H4).
      entry_kind: Number.parseInt(entryKind, 10) || 0,
      lines: resolved.lines,
      settlements: buildSettlements(),
    }
    if (cashflowActivity.trim() !== '') {
      const parsed = Number.parseInt(cashflowActivity, 10)
      if (!Number.isNaN(parsed)) {
        body.cashflow_activity = parsed
      }
    }

    if (voucher === null) {
      createMutation.mutate(
        { body: body as unknown as JournalVoucherIn, idempotencyKey, acknowledgeWarnings },
        {
          onSuccess: goToList,
          onError: (caught) => {
            // Chỉ lượt TẠO mới có đường "Vẫn ghi sổ?": cảnh báo FR-SYS-062 chỉ
            // phát trên lượt ghi sổ đi kèm khi bật Cất-đồng-thời-ghi-sổ.
            fail(caught, 'create')
          },
        },
      )
      return
    }
    updateMutation.mutate({ ...body, row_version: voucher.row_version } as unknown as JournalVoucherUpdate, {
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
      currencyCode !== 'VND' ||
      exchangeRate !== '1' ||
      cashflowActivity !== '' ||
      // Chứng từ kết chuyển phải mở sẵn khối "Mở rộng": người sửa cần thấy cờ
      // đang bật, nếu không họ sẽ không hiểu vì sao nó vắng trên báo cáo KQKD.
      entryKind !== '0')

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

      <JournalVoucherHeaderFields
        postingDate={postingDate}
        onPostingDateChange={(value) => {
          setPostingDate(value)
          if (!documentDateTouched) {
            setDocumentDate(value)
          }
        }}
        description={description}
        onDescriptionChange={setDescription}
        documentDate={documentDate}
        onDocumentDateChange={(value) => {
          setDocumentDate(value)
          setDocumentDateTouched(true)
        }}
        currencyCode={currencyCode}
        onCurrencyCodeChange={setCurrencyCode}
        exchangeRate={exchangeRate}
        onExchangeRateChange={setExchangeRate}
        cashflowActivity={cashflowActivity}
        onCashflowActivityChange={setCashflowActivity}
        entryKind={entryKind}
        onEntryKindChange={setEntryKind}
        defaultAdvancedOpen={hasAdvancedValues}
      />

      <DataGrid
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption={t('gl.line.caption')}
        cellLabel={(header, rowNumber) => t('gl.line.cellLabel', { header, row: String(rowNumber) })}
        onCommit={handleCommit}
      />

      <JournalSettlementBlock
        lines={debtLines}
        branchId={voucher !== null ? voucher.branch_id : (access.data?.acting_branch_id ?? null)}
        asOf={postingDate}
        currencyCode={currencyCode.trim() === '' ? 'VND' : currencyCode.trim()}
        amounts={settlementAmounts}
        onAmountChange={(key, value) => {
          setSettlementAmounts((current) => ({ ...current, [key]: value }))
        }}
        disabled={readOnly || busy}
      />

      <JournalVoucherActionsFooter
        voucherStatus={voucher?.status ?? null}
        voucherId={voucher?.id ?? null}
        documentType={voucher?.document_type ?? null}
        readOnly={readOnly}
        busy={busy}
        confirmDelete={confirmDelete}
        onCancel={goToList}
        // Bọc lại để `onClick` không tuồn MouseEvent vào tham số
        // `acknowledgeWarnings` của `handleSave`.
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
