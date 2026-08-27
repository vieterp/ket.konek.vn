/**
 * Nút In chứng từ (FR-RPT-008/011, lát 5E; hộp chọn mẫu — nợ 6E-2) — tự chứa:
 * gọi `POST /vouchers/{id}/print`, lưu PDF về máy, và hiện cảnh báo in lại.
 *
 * Cảnh báo đọc từ header (`X-Print-Reprint`, `X-Print-Copy-No`): in lần hai
 * trở đi là SỰ KIỆN có thật mà `print_log` đếm — client chỉ nói ra điều đó,
 * không quyết định gì.
 *
 * Hộp chọn mẫu chỉ hiện khi loại chứng từ có NHIỀU HƠN MỘT mẫu đăng ký
 * (`GET /print-templates?document_type=`): PT nay có cả 01-TT lẫn mẫu kế toán
 * chung nên "mẫu mặc định" không còn là câu trả lời duy nhất. Không truyền
 * `documentType` thì giữ hành vi cũ — in mẫu mặc định của server.
 *
 * Bản nháp vẫn in được (kèm watermark BẢN NHÁP server đóng) trừ khi đơn vị
 * tắt công tắc `print.allow_draft_vouchers` — mọi luật nằm ở server, nút này
 * hiển thị lỗi nghiệp vụ trả về y như mọi lệnh khác.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { useMutation, useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { Button, SelectField } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { saveBlob } from '@/lib/job-tracking'
import { ApiError, useSession } from '@/lib/session'

interface PrintOutcome {
  readonly copyNo: number
  readonly reprint: boolean
}

function usePrintVoucher() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async ({
      voucherId,
      templateCode,
    }: {
      readonly voucherId: string
      readonly templateCode: string | null
    }): Promise<PrintOutcome> => {
      const outcome = await client.postBlob(
        `/api/v1/vouchers/${voucherId}/print`,
        templateCode === null ? {} : { template_code: templateCode },
        { datasetCode },
      )
      if (outcome.kind !== 'file') {
        throw new Error('print trả về JSON — hợp đồng chỉ có tệp PDF')
      }
      saveBlob(outcome.blob, outcome.fileName ?? 'chung-tu.pdf')
      return {
        copyNo: Number(outcome.headers.get('X-Print-Copy-No') ?? '1'),
        reprint: outcome.headers.get('X-Print-Reprint') === 'true',
      }
    },
  })
}

/** Mẫu in đã đăng ký của một mã bản in — nguồn cho hộp chọn mẫu. */
export function usePrintTemplates(documentType: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['print-templates', datasetCode, documentType],
    enabled: datasetCode !== null && documentType !== null,
    queryFn: () =>
      client.get<Schemas['PrintTemplateListResponse']>(
        `/api/v1/print-templates?document_type=${encodeURIComponent(documentType ?? '')}`,
        { datasetCode },
      ),
  })
}

export function PrintVoucherButton({
  voucherId,
  disabled,
  documentType = null,
}: {
  readonly voucherId: string
  readonly disabled: boolean
  /** Có mã loại thì nút mới tra được danh sách mẫu — không có giữ hành vi cũ. */
  readonly documentType?: string | null
}): ReactElement {
  const { t } = useI18n()
  const print = usePrintVoucher()
  const templates = usePrintTemplates(documentType)
  const [notice, setNotice] = useState<string | null>(null)
  const [templateCode, setTemplateCode] = useState('')

  const errorMessage =
    print.error instanceof ApiError
      ? translateErrorCode(t, print.error.errorCode)
      : print.isError
        ? t('error.transport.unreachable')
        : null

  const templateRows = templates.data?.templates ?? []

  return (
    <span className="flex items-center gap-2">
      {templateRows.length > 1 && (
        <SelectField
          label={t('gl.print.templateLabel')}
          labelHidden
          value={templateCode}
          onChange={(event) => {
            setTemplateCode(event.target.value)
          }}
          options={[
            { value: '', label: t('gl.print.templateDefault') },
            ...templateRows.map((template) => ({
              value: template.code,
              label: `${template.code} — ${template.name}`,
            })),
          ]}
        />
      )}
      <Button
        variant="secondary"
        disabled={disabled || print.isPending}
        onClick={() => {
          setNotice(null)
          print.mutate(
            { voucherId, templateCode: templateCode === '' ? null : templateCode },
            {
              onSuccess: (outcome) => {
                setNotice(
                  outcome.reprint
                    ? t('gl.print.reprintWarning', { copy: String(outcome.copyNo) })
                    : null,
                )
              },
            },
          )
        }}
      >
        {t('gl.print.button')}
      </Button>
      {notice !== null ? <span className="text-xs text-amber-700">{notice}</span> : null}
      {errorMessage !== null ? <span className="text-xs text-red-700">{errorMessage}</span> : null}
    </span>
  )
}
