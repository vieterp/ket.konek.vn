/**
 * Thẻ "Logo trên báo cáo" trong màn Thiết lập — nút một-bước (FR-RPT-010,
 * lát 5E, trả nợ (d) của 5D).
 *
 * Một lượt chọn tệp = một lượt gọi `POST /system/settings/logo`: server ghi
 * blob + cả hai khóa settings trong một transaction, client không dán hash tay
 * nữa. Trạng thái "đã có logo" đọc từ chính danh sách settings (khóa
 * `report.logo_content_hash` khác rỗng) — không giữ bản sao nào ở client.
 */

import type { ReactElement } from 'react'
import { useRef, useState } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'

import { Alert, Button } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useSettings } from './use-settings-groups'

function useUploadReportLogo() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ file }: { readonly file: File }) => {
      const form = new FormData()
      form.append('file', file)
      return client.postForm<{ content_hash: string }>('/api/v1/system/settings/logo', form, {
        datasetCode,
      })
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['settings', datasetCode] })
      void queryClient.invalidateQueries({ queryKey: ['settings-groups', datasetCode] })
    },
  })
}

export function ReportLogoCard({ readOnly }: { readonly readOnly: boolean }): ReactElement {
  const { t } = useI18n()
  const settings = useSettings()
  const upload = useUploadReportLogo()
  const inputRef = useRef<HTMLInputElement>(null)
  const [done, setDone] = useState(false)

  const currentHash =
    settings.data?.find((item) => item.key === 'report.logo_content_hash')?.value ?? ''
  const hasLogo = currentHash !== ''

  const errorMessage =
    upload.error instanceof ApiError
      ? translateErrorCode(t, upload.error.errorCode)
      : upload.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <section className="rounded border border-border-default bg-background p-4">
      <h2 className="text-base font-semibold text-text-default">{t('setup.logo.title')}</h2>
      <p className="mt-1 text-sm text-text-muted">
        {hasLogo ? t('setup.logo.present') : t('setup.logo.absent')}
      </p>
      {errorMessage !== null && (
        <div className="mt-2">
          <Alert tone="error">{errorMessage}</Alert>
        </div>
      )}
      {done && !upload.isError ? (
        <p className="mt-2 text-sm text-green-700">{t('setup.logo.saved')}</p>
      ) : null}
      {!readOnly && (
        <div className="mt-3">
          <input
            ref={inputRef}
            type="file"
            accept="image/png,image/jpeg,image/svg+xml"
            className="hidden"
            data-testid="logo-file-input"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file === undefined) {
                return
              }
              setDone(false)
              upload.mutate(
                { file },
                {
                  onSuccess: () => {
                    setDone(true)
                  },
                },
              )
              // Cho phép chọn lại CÙNG tệp lần nữa (đổi rồi đổi lại) — input
              // file không bắn onChange khi value không đổi.
              event.target.value = ''
            }}
          />
          <Button
            variant="secondary"
            disabled={upload.isPending}
            onClick={() => {
              inputRef.current?.click()
            }}
          >
            {upload.isPending
              ? t('setup.logo.uploading')
              : hasLogo
                ? t('setup.logo.replace')
                : t('setup.logo.choose')}
          </Button>
        </div>
      )}
    </section>
  )
}
