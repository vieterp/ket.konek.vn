/**
 * Wizard xử lý hóa đơn sai (design nhóm 02 `#hddt-ss`, U4; FR-EIV-030..034):
 * người dùng chỉ trả lời "sai cái gì" và "khách đã kê khai chưa" — hệ thống
 * tra bảng quyết định rồi chọn thay thế / điều chỉnh / hủy.
 *
 * Câu hỏi DỰNG TỪ `GET /einvoices/error-flows` (FR-NFR-055): bước 1 liệt kê
 * các `error_kind` có trong bảng; bước 2 chỉ hiện khi nhánh ấy có dòng mang
 * `buyer_declared` khác `null`; băng "hệ thống sẽ làm giúp bạn" nói `remedy`
 * của dòng khớp. Không có cây quyết định nào viết cứng ở đây.
 *
 * Bước 3 theo nhánh (quyết định user 2026-09-18) vì server không sửa dòng
 * trên tờ hóa đơn — nội dung đúng nằm ở CHỨNG TỪ BÁN:
 * - thay thế / điều chỉnh thông tin: số + ngày 04/SS → `resolve-error` → tờ
 *   mới ở "chờ phát hành" trên cùng chứng từ; màn kết quả mở nút "Sửa chứng
 *   từ bán" (FR-EIV-035 mở khóa khi tờ cũ đã bị thay thế) rồi phát hành từ lưới;
 * - điều chỉnh tiền: chọn chứng từ bán kind 5/6 có `adjusts_voucher_id` = chứng
 *   từ gốc (hoặc lập mới qua deep-link 7H-2a) rồi 04/SS → `resolve-error` kèm
 *   `adjustment_voucher_id`;
 * - hủy: thông báo hủy + biên bản hủy (BR-EIV-04) lập và nộp ngay, rồi 04/SS
 *   → `resolve-error`.
 *
 * 04/SS bắt buộc ở mọi nhánh (`ResolveErrorIn.notice_no/notice_date`) — design
 * viết "hệ thống tự lập thông báo sai sót", nhưng số văn bản là thứ kế toán
 * đánh, phần mềm không bịa ra được.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { Alert, Button, Seg, SelectField, TextField } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import {
  SALES_KIND_ADJUSTMENT_DECREASE,
  SALES_KIND_ADJUSTMENT_INCREASE,
} from '@/features/ban-hang/sales-row-status'
import { useSalesInvoiceList } from '@/features/ban-hang/use-sales-invoices'
import { VOUCHER_STATUS_POSTED } from '@/features/so-sach-thue/voucher-status'

import { EInvoicePreviewDrawer } from './einvoice-preview-drawer'
import { asksBuyerDeclared, matchFlow } from './error-flow-match'
import { canResolveError, invoiceNumberText } from './einvoice-row-status'
import { FeatureNav } from './feature-nav'
import {
  useEInvoiceActions,
  useEInvoiceRow,
  useErrorFlows,
  type EInvoiceListItem,
  type ResolveErrorOut,
} from './use-einvoices'

/** Khớp `Remedy` phía server. */
const REMEDY_REPLACE = 0
const REMEDY_ADJUST_INFO = 1
const REMEDY_ADJUST_AMOUNT = 2
const REMEDY_CANCEL = 3

/** Khớp `ErrorNoticeKind` phía server — hai văn bản của BR-EIV-04. */
const NOTICE_KIND_CANCEL_NOTICE = 0
const NOTICE_KIND_CANCEL_RECORD = 1

/** Mã lỗi chung của ràng buộc duy nhất (`problem_details`) — văn bản cùng loại đã lập. */
const DUPLICATE_ERROR_CODE = 'data.duplicate'

const ERROR_KIND_LABEL_KEYS = {
  0: 'einvoice.wizard.kind.info',
  1: 'einvoice.wizard.kind.amount',
  2: 'einvoice.wizard.kind.none',
} as const
const ERROR_KIND_HINT_KEYS = {
  0: 'einvoice.wizard.kindHint.info',
  1: 'einvoice.wizard.kindHint.amount',
  2: 'einvoice.wizard.kindHint.none',
} as const
const REMEDY_LABEL_KEYS = {
  [REMEDY_REPLACE]: 'einvoice.wizard.remedy.replace',
  [REMEDY_ADJUST_INFO]: 'einvoice.wizard.remedy.adjustInfo',
  [REMEDY_ADJUST_AMOUNT]: 'einvoice.wizard.remedy.adjustAmount',
  [REMEDY_CANCEL]: 'einvoice.wizard.remedy.cancel',
} as const

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

interface NoticeDraft {
  readonly no: string
  readonly date: string
}

function OriginalCard({
  invoice,
  onPreview,
}: {
  readonly invoice: EInvoiceListItem
  readonly onPreview: () => void
}): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  return (
    <aside className="flex w-[300px] shrink-0 flex-col gap-2 rounded border border-border-default p-3 text-sm">
      <h2 className="font-semibold text-primary">{t('einvoice.wizard.original')}</h2>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-text-muted">{t('einvoice.list.column.no')}</dt>
        <dd className="font-semibold">{invoiceNumberText(t, invoice)}</dd>
        <dt className="text-text-muted">{t('einvoice.list.column.date')}</dt>
        <dd>{invoice.invoice_date === null ? '—' : formatDate(invoice.invoice_date, locale)}</dd>
        <dt className="text-text-muted">{t('einvoice.list.column.customer')}</dt>
        <dd>{invoice.customer_name ?? invoice.customer_code ?? ''}</dd>
        <dt className="text-text-muted">{t('einvoice.list.column.total')}</dt>
        <dd className="tabular-nums">{formatMoney(invoice.total_fc, locale)}</dd>
        <dt className="text-text-muted">{t('einvoice.list.column.voucher')}</dt>
        <dd>
          <button
            type="button"
            className="text-secondary hover:underline"
            onClick={() => {
              void navigate(`/ban-hang/chung-tu/${invoice.source_voucher_id}`)
            }}
          >
            {invoice.voucher_no}
          </button>
        </dd>
      </dl>
      <Button variant="secondary" onClick={onPreview}>
        {t('einvoice.wizard.viewPdf')}
      </Button>
    </aside>
  )
}

function NoticeFields({
  legend,
  value,
  onChange,
}: {
  readonly legend: string
  readonly value: NoticeDraft
  readonly onChange: (next: NoticeDraft) => void
}): ReactElement {
  const { t } = useI18n()
  return (
    <fieldset className="flex flex-wrap items-end gap-3">
      <legend className="mb-1 text-sm font-semibold text-primary">{legend}</legend>
      <TextField
        label={t('einvoice.wizard.noticeNo')}
        value={value.no}
        onChange={(event) => {
          onChange({ ...value, no: event.target.value })
        }}
      />
      <TextField
        label={t('einvoice.wizard.noticeDate')}
        type="date"
        value={value.date}
        onChange={(event) => {
          onChange({ ...value, date: event.target.value })
        }}
      />
    </fieldset>
  )
}

/** Bước 3 nhánh điều chỉnh tiền: chứng từ bán mang phần chênh, đã lập cho đúng chứng từ gốc. */
function DeltaVoucherPicker({
  invoice,
  value,
  onChange,
}: {
  readonly invoice: EInvoiceListItem
  readonly value: string
  readonly onChange: (id: string) => void
}): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  // Lọc ở server theo chứng từ gốc (7H-3 M-4) và chỉ chứng từ ĐÃ GHI SỔ —
  // `_verify_delta_voucher` từ chối chứng từ chưa ghi sổ, nên không bày ra.
  const list = useSalesInvoiceList({
    page: 1,
    adjustsVoucherId: invoice.source_voucher_id,
    status: VOUCHER_STATUS_POSTED,
  })
  const candidates = list.data?.items ?? []
  return (
    <div className="flex flex-col gap-2">
      <SelectField
        label={t('einvoice.wizard.deltaVoucher')}
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
        }}
        options={[
          { value: '', label: t('einvoice.wizard.deltaVoucherPlaceholder') },
          ...candidates.map((row) => ({
            value: row.id,
            label: `${row.voucher_no} · ${formatDate(row.posting_date, locale)} · ${formatMoney(row.total_fc, locale)}`,
          })),
        ]}
      />
      <p className="text-xs text-text-muted">{t('einvoice.wizard.deltaVoucherHint')}</p>
      <div className="flex gap-2">
        <Button
          variant="secondary"
          onClick={() => {
            void navigate(
              `/ban-hang/chung-tu/moi?kind=${String(SALES_KIND_ADJUSTMENT_INCREASE)}&adjusts_voucher_id=${invoice.source_voucher_id}`,
            )
          }}
        >
          {t('einvoice.wizard.createIncrease')}
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            void navigate(
              `/ban-hang/chung-tu/moi?kind=${String(SALES_KIND_ADJUSTMENT_DECREASE)}&adjusts_voucher_id=${invoice.source_voucher_id}`,
            )
          }}
        >
          {t('einvoice.wizard.createDecrease')}
        </Button>
      </div>
    </div>
  )
}

function Outcome({
  outcome,
  invoice,
}: {
  readonly outcome: ResolveErrorOut
  readonly invoice: EInvoiceListItem
}): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const successor = outcome.replacement ?? outcome.adjustment ?? null
  return (
    <div className="flex flex-col gap-3">
      <Alert tone="info">
        {t('einvoice.wizard.done', { remedy: t(REMEDY_LABEL_KEYS[outcome.remedy]) })}
      </Alert>
      <p className="text-sm text-text-default">
        {t('einvoice.wizard.noticeFiled', { no: outcome.notice.notice_no })}
      </p>
      {successor !== null && (
        <p className="text-sm text-text-default">
          {t(
            outcome.remedy === REMEDY_ADJUST_AMOUNT
              ? 'einvoice.wizard.successorDelta'
              : 'einvoice.wizard.successorSameVoucher',
          )}
        </p>
      )}
      <div className="flex gap-2">
        {successor !== null && outcome.remedy !== REMEDY_ADJUST_AMOUNT && (
          <Button
            onClick={() => {
              void navigate(`/ban-hang/chung-tu/${invoice.source_voucher_id}`)
            }}
          >
            {t('einvoice.wizard.fixVoucher')}
          </Button>
        )}
        <Button
          variant="secondary"
          onClick={() => {
            void navigate('/hoa-don-dien-tu')
          }}
        >
          {t('einvoice.wizard.backToList')}
        </Button>
      </div>
    </div>
  )
}

export function ErrorWizard(): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const { id = null } = useParams<{ id: string }>()
  const { readOnly } = useSession()
  const { row: invoice, isPending, error: loadError } = useEInvoiceRow(id)
  const flows = useErrorFlows()
  const actions = useEInvoiceActions()

  const [errorKind, setErrorKind] = useState<number | null>(null)
  const [buyerDeclared, setBuyerDeclared] = useState<boolean | null>(null)
  const [notice, setNotice] = useState<NoticeDraft>({ no: '', date: todayIso() })
  const [reason, setReason] = useState('')
  const [deltaVoucherId, setDeltaVoucherId] = useState('')
  const [cancelNotice, setCancelNotice] = useState<NoticeDraft>({ no: '', date: todayIso() })
  const [cancelRecord, setCancelRecord] = useState<NoticeDraft>({ no: '', date: todayIso() })
  const [error, setError] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<ResolveErrorOut | null>(null)
  const [previewing, setPreviewing] = useState(false)

  const table = flows.data ?? []
  const flow = matchFlow(table, errorKind, buyerDeclared)
  const remedy = flow?.remedy ?? null
  const asks = asksBuyerDeclared(table, errorKind)
  const kinds = [...new Set(table.map((row) => row.error_kind))].sort((a, b) => a - b)
  const busy = actions.resolveError.isPending || actions.addNotice.isPending

  function describe(caught: unknown): string {
    return caught instanceof ApiError
      ? translateErrorCode(t, caught.errorCode)
      : t('error.transport.unreachable')
  }

  async function submit(): Promise<void> {
    if (invoice === null || flow === null || remedy === null) {
      return
    }
    if (notice.no.trim() === '' || notice.date === '') {
      setError(t('einvoice.wizard.noticeRequired'))
      return
    }
    if (remedy === REMEDY_ADJUST_AMOUNT && deltaVoucherId === '') {
      setError(t('einvoice.wizard.deltaVoucherRequired'))
      return
    }
    if (
      remedy === REMEDY_CANCEL &&
      [cancelNotice, cancelRecord].some((draft) => draft.no.trim() === '' || draft.date === '')
    ) {
      setError(t('einvoice.wizard.cancelDocsRequired'))
      return
    }
    setError(null)
    try {
      if (remedy === REMEDY_CANCEL) {
        // BR-EIV-04: cạnh hủy đòi cả hai văn bản ĐÃ NỘP trước khi `resolve` chạy.
        // Hai lượt POST không nằm trong một giao dịch: lượt trước có thể đã
        // xong khi lượt sau lỗi (422, mất mạng), và gửi lại thì văn bản cùng
        // loại đã có → 409 `data.duplicate` (chỉ mục `(einvoice_id, kind)`).
        // Coi 409 ấy là "đã lập" và đi tiếp — văn bản đã nộp, ngữ nghĩa đúng
        // (review 7H-3 H-2).
        for (const [kind, draft] of [
          [NOTICE_KIND_CANCEL_NOTICE, cancelNotice],
          [NOTICE_KIND_CANCEL_RECORD, cancelRecord],
        ] as const) {
          try {
            await actions.addNotice.mutateAsync({
              id: invoice.id,
              body: {
                kind,
                notice_no: draft.no.trim(),
                notice_date: draft.date,
                reason_code: null,
                reason: reason.trim() === '' ? null : reason.trim(),
                submitted: true,
              },
            })
          } catch (caught) {
            if (!(caught instanceof ApiError && caught.errorCode === DUPLICATE_ERROR_CODE)) {
              throw caught
            }
          }
        }
      }
      const result = await actions.resolveError.mutateAsync({
        id: invoice.id,
        body: {
          error_kind: flow.error_kind,
          buyer_declared: asks ? buyerDeclared : null,
          notice_no: notice.no.trim(),
          notice_date: notice.date,
          reason: reason.trim() === '' ? null : reason.trim(),
          adjustment_voucher_id: remedy === REMEDY_ADJUST_AMOUNT ? deltaVoucherId : null,
        },
      })
      setOutcome(result)
    } catch (caught) {
      setError(describe(caught))
    }
  }

  const loadMessage =
    loadError instanceof ApiError
      ? translateErrorCode(t, loadError.errorCode)
      : loadError !== null && loadError !== undefined
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex items-baseline justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">
            {invoice === null
              ? t('einvoice.wizard.titleBare')
              : t('einvoice.wizard.title', {
                  no: invoiceNumberText(t, invoice),
                  customer: invoice.customer_name ?? '',
                })}
          </h1>
          <Button
            variant="ghost"
            onClick={() => {
              void navigate('/hoa-don-dien-tu')
            }}
          >
            {t('common.cancel')}
          </Button>
        </header>
        {loadMessage !== null && <Alert tone="error">{loadMessage}</Alert>}
        {isPending && invoice === null && (
          <p className="text-sm text-text-muted">{t('common.loading')}</p>
        )}
        {invoice !== null && !canResolveError(invoice) && outcome === null && (
          <Alert tone="warning">{t('einvoice.wizard.notResolvable')}</Alert>
        )}
        {invoice !== null && (
          <div className="flex gap-4">
            <div className="flex min-w-0 flex-1 flex-col gap-4">
              {outcome !== null ? (
                <Outcome outcome={outcome} invoice={invoice} />
              ) : (
                <>
                  {error !== null && <Alert tone="error">{error}</Alert>}

                  <fieldset className="flex flex-col gap-2">
                    <legend className="mb-1 text-sm font-semibold text-primary">
                      {t('einvoice.wizard.step1')}
                    </legend>
                    {kinds.map((kind) => (
                      <label key={kind} className="flex cursor-pointer items-start gap-2 text-sm">
                        <input
                          type="radio"
                          name="error-kind"
                          checked={errorKind === kind}
                          onChange={() => {
                            setErrorKind(kind)
                            setBuyerDeclared(null)
                          }}
                        />
                        <span>
                          <span className="font-medium">
                            {t(ERROR_KIND_LABEL_KEYS[kind])}
                          </span>
                          <span className="block text-xs text-text-muted">
                            {t(ERROR_KIND_HINT_KEYS[kind])}
                          </span>
                        </span>
                      </label>
                    ))}
                  </fieldset>

                  {asks && (
                    <div className="flex flex-col gap-1">
                      <span className="text-sm font-semibold text-primary">
                        {t('einvoice.wizard.step2')}
                      </span>
                      <Seg
                        label={t('einvoice.wizard.step2')}
                        value={buyerDeclared === null ? '' : buyerDeclared ? 'yes' : 'no'}
                        onChange={(value) => {
                          setBuyerDeclared(value === 'yes')
                        }}
                        options={[
                          { value: 'no', label: t('einvoice.wizard.notDeclared') },
                          { value: 'yes', label: t('einvoice.wizard.declared') },
                        ]}
                      />
                      <p className="text-xs text-text-muted">{t('einvoice.wizard.step2Hint')}</p>
                    </div>
                  )}

                  {flow !== null && remedy !== null && (
                    <div className="flex flex-col gap-3 rounded border border-primary/40 bg-navy-50 p-3">
                      <p className="text-sm font-semibold text-primary">
                        {t('einvoice.wizard.willDo', { remedy: t(REMEDY_LABEL_KEYS[remedy]) })}
                      </p>
                      <p className="text-xs text-text-muted">{flow.legal_basis}</p>
                      <p className="text-sm text-text-default">
                        {t(
                          remedy === REMEDY_ADJUST_AMOUNT
                            ? 'einvoice.wizard.step3.delta'
                            : remedy === REMEDY_CANCEL
                              ? 'einvoice.wizard.step3.cancel'
                              : 'einvoice.wizard.step3.supersede',
                        )}
                      </p>
                      {remedy === REMEDY_ADJUST_AMOUNT && (
                        <DeltaVoucherPicker
                          invoice={invoice}
                          value={deltaVoucherId}
                          onChange={setDeltaVoucherId}
                        />
                      )}
                      {remedy === REMEDY_CANCEL && (
                        <>
                          <NoticeFields
                            legend={t('einvoice.wizard.cancelNotice')}
                            value={cancelNotice}
                            onChange={setCancelNotice}
                          />
                          <NoticeFields
                            legend={t('einvoice.wizard.cancelRecord')}
                            value={cancelRecord}
                            onChange={setCancelRecord}
                          />
                        </>
                      )}
                      <NoticeFields
                        legend={t('einvoice.wizard.errorNotice')}
                        value={notice}
                        onChange={setNotice}
                      />
                      <TextField
                        label={t('einvoice.wizard.reason')}
                        value={reason}
                        onChange={(event) => {
                          setReason(event.target.value)
                        }}
                      />
                      <div>
                        <Button
                          disabled={busy || readOnly || !canResolveError(invoice)}
                          onClick={() => {
                            void submit()
                          }}
                        >
                          {t('einvoice.wizard.submit', { remedy: t(REMEDY_LABEL_KEYS[remedy]) })}
                        </Button>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
            <OriginalCard
              invoice={invoice}
              onPreview={() => {
                setPreviewing(true)
              }}
            />
          </div>
        )}
      </section>
      <EInvoicePreviewDrawer
        invoice={previewing ? invoice : null}
        onClose={() => {
          setPreviewing(false)
        }}
      />
    </div>
  )
}
