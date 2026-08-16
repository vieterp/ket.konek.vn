/**
 * Chọn dữ liệu kế toán (FR-SYS-001).
 *
 * Bắt buộc phải có vì server **không** đoán hộ: `X-Dataset` không có mặc định
 * (quyết định E1) — đoán sai dataset là ghi sổ nhầm doanh nghiệp, và đó là loại
 * sai không ai phát hiện cho tới kỳ quyết toán.
 *
 * Bản cài chỉ có **một** bộ sổ — phần lớn khách hàng — thì màn hình này tự chọn
 * và không bao giờ hiện ra: hỏi một câu chỉ có một câu trả lời là ma sát thuần.
 */

import type { ReactElement } from 'react'
import { useEffect } from 'react'

import { Alert, Button } from '@/design-system/components'
import { useDatasets } from '@/lib/access'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

export function DatasetPickerPage(): ReactElement {
  const { t } = useI18n()
  const { selectDataset, rejectedDatasetCode, logout } = useSession()
  const datasets = useDatasets()

  const items = datasets.data ?? []
  const onlyOne = items.length === 1 ? items[0] : undefined

  useEffect(() => {
    // Không tự chọn lại bộ sổ mà người dùng vừa rời khỏi vì không có quyền:
    // ở bản cài chỉ có một dữ liệu kế toán, làm thế tạo một vòng lặp không lối
    // ra (chọn → bị từ chối → quay lại → chọn lại).
    if (onlyOne !== undefined && onlyOne.code !== rejectedDatasetCode) {
      selectDataset(onlyOne.code)
    }
  }, [onlyOne, rejectedDatasetCode, selectDataset])

  return (
    <div className="flex min-h-screen items-center justify-center bg-screen p-6">
      <section className="w-full max-w-lg rounded border-2 border-navy-700 bg-white p-6">
        <h1 className="mb-2 text-lg font-semibold text-navy-700">{t('dataset.title')}</h1>
        <p className="mb-4 text-sm text-text-muted">{t('dataset.intro')}</p>

        {datasets.isPending && <p className="text-sm text-text-muted">{t('common.loading')}</p>}
        {datasets.isError && <Alert tone="error">{t('error.transport.unreachable')}</Alert>}
        {datasets.isSuccess && items.length === 0 && (
          <Alert tone="warning">{t('dataset.empty')}</Alert>
        )}

        <ul className="flex flex-col gap-2">
          {items.map((dataset) => (
            <li key={dataset.code}>
              <Button
                variant="secondary"
                className="w-full justify-between"
                onClick={() => {
                  selectDataset(dataset.code)
                }}
              >
                <span>{dataset.name}</span>
                <span className="text-xs text-text-muted">
                  {dataset.code} · {dataset.scheme}
                </span>
              </Button>
            </li>
          ))}
        </ul>

        <div className="mt-4">
          <Button
            variant="ghost"
            onClick={() => {
              void logout()
            }}
          >
            {t('common.signOut')}
          </Button>
        </div>
      </section>
    </div>
  )
}
