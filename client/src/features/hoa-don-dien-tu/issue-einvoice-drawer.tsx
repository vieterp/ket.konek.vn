/**
 * Hộp "Phát hành hóa đơn" (U3 việc tiếp theo, và đường `?source_voucher_id=`
 * từ lưới bán 7H-2a).
 *
 * Hai bước của server gói thành một hộp: **lập nháp** (`POST /einvoices` —
 * chọn ký hiệu) rồi **phát hành** (`POST …/actions/issue` — ngày hóa đơn, cấp
 * số, vào hàng đợi). Bước lập bỏ qua khi chứng từ đã có một tờ chưa phát hành
 * hoặc phát hành lỗi (`GET /einvoices?source_voucher_id=`): hai tờ cho một
 * chứng từ là 409 ở server, và hỏi lại ký hiệu của một tờ đã có là hỏi thừa.
 *
 * Ký hiệu tra danh mục `invoice_forms` ở chế độ **phẳng** (`search=`, toàn cây,
 * chỉ dòng đang hoạt động) — danh mục là cây, và một ký hiệu nằm trong nhóm
 * "HĐ GTGT" phải tìm được như ký hiệu ở gốc (review 7H-3 H-1). Chưa gõ gì thì
 * hiện lớp gốc. Hộp lọc lá · điện tử phía client trên hàng đầy đủ (`kind`,
 * `is_group`) — `_require_usable_form` phía server kiểm đúng ba điều ấy, nên ô
 * chọn không bày ra thứ bấm vào là 422. Ngày hóa đơn mặc định hôm nay;
 * BR-EIV-03 (ngày ≥ ngày đăng ký) là luật server, hộp chỉ hiện lỗi trả về.
 *
 * Chứng từ đã có tờ **còn hiệu lực** (đang phát hành / đã cấp mã / đã gửi) →
 * hộp nói ra và không có nút: lập tờ thứ hai là 409 ở server
 * (`uq_einvoices_live_source_voucher`) với câu lỗi chung không nói được vì sao.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import type { LookupOption } from '@/design-system/components'
import { Alert, Button, Drawer, LookupInput, TextField } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import type { CatalogRow } from '@/features/danh-muc-thiet-lap/catalog-types'

import {
  EINVOICE_STATUS_DRAFT,
  EINVOICE_STATUS_ISSUE_FAILED,
  LIVE_STATUSES,
  invoiceNumberText,
} from './einvoice-row-status'
import { useEInvoiceActions, useEInvoiceList, type EInvoiceListItem } from './use-einvoices'

/** Khớp `InvoiceFormKind.DIEN_TU` — loại ký hiệu xuất hóa đơn điện tử. */
const FORM_KIND_ELECTRONIC = 0

interface InvoiceFormRow extends CatalogRow {
  readonly form_no: string | null
  readonly kind: number | null
  readonly provider_code: string | null
}

const SEARCH_LIMIT = 50

function usable(rows: readonly InvoiceFormRow[]): InvoiceFormRow[] {
  return rows.filter((row) => !row.is_group && row.is_active && row.kind === FORM_KIND_ELECTRONIC)
}

/** Ký hiệu điện tử đang dùng: lớp gốc khi chưa gõ, tra phẳng toàn cây khi gõ. */
function useUsableInvoiceForms(needle: string) {
  const { client, datasetCode } = useSession()
  const trimmed = needle.trim()
  const url =
    trimmed === ''
      ? '/api/v1/master/invoice_forms?limit=200'
      : `/api/v1/master/invoice_forms?search=${encodeURIComponent(trimmed)}&limit=${String(SEARCH_LIMIT)}`
  return useQuery({
    queryKey: ['catalog', datasetCode, 'invoice_forms', 'usable', trimmed],
    enabled: datasetCode !== null,
    queryFn: async () => {
      const page = await client.get<{ readonly items: readonly InvoiceFormRow[] }>(url, {
        datasetCode,
      })
      return usable(page.items)
    },
  })
}

function optionOf(row: InvoiceFormRow): LookupOption {
  return { id: row.id, code: row.code, label: `${row.form_no ?? ''} · ${row.name}` }
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}

export interface IssueEInvoiceDrawerProps {
  readonly open: boolean
  readonly sourceVoucherId: string
  readonly onClose: () => void
  /** Gọi sau khi phát hành xong — chỗ gọi làm mới lưới / rời đường deep-link. */
  readonly onIssued: (invoice: EInvoiceListItem | null) => void
}

export function IssueEInvoiceDrawer(props: IssueEInvoiceDrawerProps): ReactElement {
  const { t } = useI18n()
  const existing = useEInvoiceList(
    { page: 1, sourceVoucherId: props.sourceVoucherId },
    props.open,
  )
  const [needle, setNeedle] = useState('')
  const forms = useUsableInvoiceForms(needle)
  const actions = useEInvoiceActions()

  const [form, setForm] = useState<LookupOption | null>(null)
  const [invoiceDate, setInvoiceDate] = useState(todayIso)
  const [error, setError] = useState<string | null>(null)
  // Sinh lúc mở hộp: bấm "Phát hành" hai lần là MỘT lượt lập và MỘT lượt cấp số.
  const [keys] = useState(() => ({ create: newIdempotencyKey(), issue: newIdempotencyKey() }))

  const pendingInvoice =
    existing.data?.items.find(
      (row) => row.status === EINVOICE_STATUS_DRAFT || row.status === EINVOICE_STATUS_ISSUE_FAILED,
    ) ?? null
  const liveInvoice =
    existing.data?.items.find((row) => LIVE_STATUSES.includes(row.status)) ?? null
  const formOptions = (forms.data ?? []).map(optionOf)
  const busy = actions.create.isPending || actions.issue.isPending

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : t('error.transport.unreachable'),
    )
  }

  async function submit(): Promise<void> {
    setError(null)
    try {
      let invoiceId = pendingInvoice?.id ?? null
      if (invoiceId === null) {
        if (form === null) {
          setError(t('einvoice.issue.formRequired'))
          return
        }
        const created = await actions.create.mutateAsync({
          sourceVoucherId: props.sourceVoucherId,
          invoiceFormId: form.id,
          idempotencyKey: keys.create,
        })
        invoiceId = created.id
      }
      await actions.issue.mutateAsync({
        id: invoiceId,
        invoiceDate,
        idempotencyKey: keys.issue,
      })
      props.onIssued(pendingInvoice)
    } catch (caught) {
      fail(caught)
    }
  }

  return (
    <Drawer
      open={props.open}
      title={t('einvoice.issue.title')}
      onClose={props.onClose}
      closeLabel={t('common.close')}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={props.onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          {liveInvoice === null && (
            <Button
              onClick={() => {
                void submit()
              }}
              disabled={busy || existing.isPending}
            >
              {t('einvoice.issue.submit')}
            </Button>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        {error !== null && <Alert tone="error">{error}</Alert>}
        {liveInvoice !== null ? (
          <Alert tone="warning">
            {t('einvoice.issue.alreadyLive', { no: invoiceNumberText(t, liveInvoice) })}
          </Alert>
        ) : pendingInvoice !== null ? (
          <p className="text-sm text-text-default">
            {t(
              pendingInvoice.status === EINVOICE_STATUS_ISSUE_FAILED
                ? 'einvoice.issue.reissueHint'
                : 'einvoice.issue.draftHint',
              { serial: pendingInvoice.serial, no: invoiceNumberText(t, pendingInvoice) },
            )}
          </p>
        ) : (
          <>
            <LookupInput
              label={t('einvoice.issue.form')}
              value={form}
              onChange={setForm}
              options={formOptions}
              onQueryChange={setNeedle}
              placeholder={t('einvoice.issue.formPlaceholder')}
              clearLabel={t('common.close')}
              emptyLabel={t('einvoice.issue.noForms')}
            />
            <p className="text-xs text-text-muted">{t('einvoice.issue.formHint')}</p>
          </>
        )}
        <TextField
          label={t('einvoice.issue.date')}
          type="date"
          value={invoiceDate}
          onChange={(event) => {
            setInvoiceDate(event.target.value)
          }}
        />
        <p className="text-xs text-text-muted">{t('einvoice.issue.explain')}</p>
      </div>
    </Drawer>
  )
}
