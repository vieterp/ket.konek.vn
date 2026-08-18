/**
 * Nạp con trực tiếp của một nút danh mục — nguồn dữ liệu cho mọi `TreePicker`
 * trong nhóm màn hình này (cây điều hướng, ô chọn nhóm cha, ô chọn kho).
 *
 * Tách khỏi `catalog-field-inputs.tsx` để tệp đó chỉ export component (yêu cầu
 * của react-refresh) — hook và component trộn một tệp làm hot-reload phải nạp
 * lại cả trang.
 */

import { useCallback } from 'react'

import type { TreePickerNode } from '@/design-system/components'
import { useSession } from '@/lib/session'

import type { CatalogPage } from './catalog-types'

export function useLoadChildren(
  slug: string,
): (parentId: number | null) => Promise<readonly TreePickerNode[]> {
  const { client, datasetCode } = useSession()

  return useCallback(
    async (parentId: number | null) => {
      const query = parentId === null ? '' : `?parent_id=${String(parentId)}`
      const page = await client.get<CatalogPage>(`/api/v1/master/${slug}${query}`, {
        datasetCode,
      })
      return page.items.map((row) => ({
        id: row.id,
        code: row.code,
        label: row.name,
        isGroup: row.is_group,
      }))
    },
    [client, datasetCode, slug],
  )
}
