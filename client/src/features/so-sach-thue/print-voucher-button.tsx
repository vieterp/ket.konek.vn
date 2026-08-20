/**
 * Nút In chứng từ (FR-RPT-008/011, lát 5E) — tự chứa: gọi
 * `POST /vouchers/{id}/print`, lưu PDF về máy, và hiện cảnh báo in lại.
 *
 * Cảnh báo đọc từ header (`X-Print-Reprint`, `X-Print-Copy-No`): in lần hai
 * trở đi là SỰ KIỆN có thật mà `print_log` đếm — client chỉ nói ra điều đó,
 * không quyết định gì. Không truyền `template_code` — server dùng mẫu mặc
 * định của loại chứng từ; ô chọn mẫu đến khi một loại có nhiều hơn một mẫu
 * dùng thật (YAGNI).
 *
 * Bản nháp vẫn in được (kèm watermark BẢN NHÁP server đóng) trừ khi đơn vị
 * tắt công tắc `print.allow_draft_vouchers` — mọi luật nằm ở server, nút này
 * hiển thị lỗi nghiệp vụ trả về y như mọi lệnh khác.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { useMutation } from '@tanstack/react-query'

import { Button } from '@/design-system/components'
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
    mutationFn: async ({ voucherId }: { readonly voucherId: string }): Promise<PrintOutcome> => {
      const outcome = await client.postBlob(
        `/api/v1/vouchers/${voucherId}/print`,
        {},
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

export function PrintVoucherButton({
  voucherId,
  disabled,
}: {
  readonly voucherId: string
  readonly disabled: boolean
}): ReactElement {
  const { t } = useI18n()
  const print = usePrintVoucher()
  const [notice, setNotice] = useState<string | null>(null)

  const errorMessage =
    print.error instanceof ApiError
      ? translateErrorCode(t, print.error.errorCode)
      : print.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <span className="flex items-center gap-2">
      <Button
        variant="secondary"
        disabled={disabled || print.isPending}
        onClick={() => {
          setNotice(null)
          print.mutate(
            { voucherId },
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
