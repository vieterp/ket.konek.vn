/**
 * Drawer xem trước bản thể hiện hóa đơn (FR-EIV-016/026) + cạnh "đã gửi"
 * (7E-3, FR-EIV-020).
 *
 * PDF đi qua `GET /einvoices/{id}/representation?kind=pdf` → `Blob` → URL
 * tạm trong `<iframe>` (thu hồi khi đóng — mỗi blob URL giữ tệp trong RAM
 * chừng nào còn sống). Hai nút tải PDF/XML dùng chính đường ấy với `saveBlob`.
 * `503` là "nhà cung cấp chưa trả bản thể hiện — thử lại sau" (lượt đầu phải
 * lấy từ họ), không phải lỗi đỏ.
 *
 * Nút "Đã gửi khách" KHÔNG gửi gì (quyết định user 2026-09-08): kế toán gửi
 * từ hộp thư của mình rồi ghi nhận ở đây — `mark-sent` chỉ ghi `sent_to` và
 * lật cạnh `DA_GUI`. Trạng thái ấy đứng CẠNH nút tải (design: "trạng thái cạnh
 * nút tải") để người in tờ giấy biết khách đã có bản điện tử chưa.
 */

import type { ReactElement } from 'react'
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { Alert, Button, Drawer, TextField } from '@/design-system/components'
import { formatDateTime } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { saveBlob } from '@/lib/job-tracking'
import { ApiError, useSession } from '@/lib/session'

import { EINVOICE_STATUS_ISSUED, invoiceNumberText } from './einvoice-row-status'
import { useEInvoiceActions, type EInvoiceListItem } from './use-einvoices'

const REPRESENTATION_UNAVAILABLE = 503

function representationPath(id: string, kind: 'pdf' | 'xml'): string {
  return `/api/v1/einvoices/${id}/representation?kind=${kind}`
}

export interface EInvoicePreviewDrawerProps {
  readonly invoice: EInvoiceListItem | null
  readonly onClose: () => void
}

export function EInvoicePreviewDrawer(props: EInvoicePreviewDrawerProps): ReactElement {
  const { t, locale } = useI18n()
  const { client, datasetCode, readOnly } = useSession()
  const actions = useEInvoiceActions()
  const invoice = props.invoice
  const invoiceId = invoice?.id ?? null

  const pdf = useQuery({
    queryKey: ['einvoices', datasetCode, 'representation', invoiceId],
    enabled: datasetCode !== null && invoiceId !== null,
    retry: false,
    queryFn: () => client.getBlob(representationPath(String(invoiceId), 'pdf'), { datasetCode }),
  })

  // URL tạm sống cùng blob đang xem; đổi tờ hay đóng drawer là thu hồi.
  const blob = pdf.data?.blob ?? null
  const objectUrl = useMemo(() => (blob === null ? null : URL.createObjectURL(blob)), [blob])
  useEffect(
    () => () => {
      if (objectUrl !== null) {
        URL.revokeObjectURL(objectUrl)
      }
    },
    [objectUrl],
  )

  const [sentTo, setSentTo] = useState('')
  const [sendError, setSendError] = useState<string | null>(null)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  function describe(caught: unknown): string {
    return caught instanceof ApiError
      ? translateErrorCode(t, caught.errorCode)
      : t('error.transport.unreachable')
  }

  async function download(kind: 'pdf' | 'xml'): Promise<void> {
    if (invoice === null) {
      return
    }
    setDownloadError(null)
    try {
      const file = await client.getBlob(representationPath(invoice.id, kind), { datasetCode })
      saveBlob(file.blob, file.fileName ?? `${invoice.serial}-${invoice.invoice_no ?? ''}.${kind}`)
    } catch (caught) {
      setDownloadError(describe(caught))
    }
  }

  function markSent(): void {
    if (invoice === null || sentTo.trim() === '') {
      setSendError(t('einvoice.preview.sentToRequired'))
      return
    }
    setSendError(null)
    actions.markSent.mutate(
      { id: invoice.id, sentTo: sentTo.trim() },
      {
        // Hàng trong prop là ảnh chụp lúc mở — đóng để lưới vẽ lại tờ ở "đã
        // gửi", thay vì để nút bấm lần hai đâm 409 (review 7H-3 M-1).
        onSuccess: props.onClose,
        onError: (caught) => {
          setSendError(describe(caught))
        },
      },
    )
  }

  const unavailable =
    pdf.error instanceof ApiError && pdf.error.status === REPRESENTATION_UNAVAILABLE
  const pdfError = pdf.isError && !unavailable ? describe(pdf.error) : null

  return (
    <Drawer
      open={invoice !== null}
      title={invoice === null ? '' : t('einvoice.preview.title', { no: invoiceNumberText(t, invoice) })}
      onClose={props.onClose}
      closeLabel={t('common.close')}
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          {/* Trạng thái gửi đứng cạnh nút tải — 7E-3. */}
          <span className="text-xs text-text-muted">
            {invoice?.sent_at != null
              ? t('einvoice.preview.sentStatus', {
                  to: invoice.sent_to ?? '',
                  at: formatDateTime(invoice.sent_at, locale),
                })
              : t('einvoice.preview.notSentStatus')}
          </span>
          <span className="flex gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                void download('pdf')
              }}
            >
              {t('einvoice.preview.downloadPdf')}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                void download('xml')
              }}
            >
              {t('einvoice.preview.downloadXml')}
            </Button>
          </span>
        </div>
      }
    >
      <div className="flex h-full flex-col gap-3">
        {downloadError !== null && <Alert tone="error">{downloadError}</Alert>}
        {pdfError !== null && <Alert tone="error">{pdfError}</Alert>}
        {unavailable && <Alert tone="warning">{t('einvoice.preview.unavailable')}</Alert>}
        {pdf.isPending && <p className="text-sm text-text-muted">{t('common.loading')}</p>}
        {objectUrl !== null && (
          <iframe
            title={t('einvoice.preview.frameTitle')}
            src={objectUrl}
            className="min-h-[520px] w-full flex-1 rounded border border-border-default bg-white"
          />
        )}
        {!readOnly && invoice?.status === EINVOICE_STATUS_ISSUED && (
          <div className="flex flex-col gap-2 rounded border border-border-default p-3">
            <p className="text-sm font-semibold text-primary">{t('einvoice.preview.markSentTitle')}</p>
            <p className="text-xs text-text-muted">{t('einvoice.preview.markSentHint')}</p>
            {sendError !== null && <Alert tone="error">{sendError}</Alert>}
            <div className="flex items-end gap-2">
              <TextField
                label={t('einvoice.preview.sentTo')}
                value={sentTo}
                onChange={(event) => {
                  setSentTo(event.target.value)
                }}
                className="flex-1"
              />
              <Button onClick={markSent} disabled={actions.markSent.isPending}>
                {t('einvoice.preview.markSent')}
              </Button>
            </div>
          </div>
        )}
      </div>
    </Drawer>
  )
}
