/**
 * Trọn danh sách bản ghi **chọn được** của một danh mục tham chiếu nhỏ — nguồn
 * cho `LookupInput` và cho việc tra tên (ngân hàng trên bảng tài khoản).
 *
 * Server không có đường đọc phẳng (danh sách là `children_of`/`subtree_of` —
 * 3B-1), nên chỉ đọc trang gốc là bỏ sót mọi bản ghi nằm trong nhóm (review 3D
 * H-3: điều khoản thanh toán xếp trong nhóm thành không chọn được). Hook này
 * duyệt đủ: một lượt tầng gốc + một lượt `subtree_of` cho **mỗi nhóm gốc** —
 * cả nhánh về trong một câu, nên số lượt gọi = 1 + số nhóm ở gốc.
 *
 * Chỉ dành cho danh mục đích nhỏ (điều khoản, ngân hàng, đơn vị tính — trần
 * 200/lượt của H52 là đủ). Danh mục lớn/phân cấp sâu chọn qua `TreePicker`.
 * Khóa truy vấn nằm dưới tiền tố `['catalog', dataset, slug]` nên mọi lệnh ghi
 * vào danh mục đó tự làm mới luôn danh sách này.
 */

import { useQuery } from '@tanstack/react-query'

import type { LookupOption } from '@/design-system/components'
import { useSession } from '@/lib/session'

import type { CatalogPage, CatalogRow } from './catalog-types'

/** Trần một lượt đọc — bằng MAX_PAGE_SIZE của server (H52). */
const FETCH_LIMIT = 200

export function useLookupOptions(slug: string) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['catalog', datasetCode, slug, 'lookup-options'],
    enabled: datasetCode !== null,
    queryFn: async (): Promise<readonly LookupOption[]> => {
      const roots = await client.get<CatalogPage>(
        `/api/v1/master/${slug}?limit=${String(FETCH_LIMIT)}`,
        { datasetCode },
      )
      const rows: CatalogRow[] = [...roots.items]
      for (const group of roots.items.filter((row) => row.is_group)) {
        const branch = await client.get<CatalogPage>(
          `/api/v1/master/${slug}?subtree_of=${String(group.id)}&limit=${String(FETCH_LIMIT)}`,
          { datasetCode },
        )
        rows.push(...branch.items)
      }
      // `subtree_of` trả cả nút neo nên phải khử trùng. Nút nhóm bị loại (không
      // phải đích tham chiếu); bản ghi ngừng theo dõi thì **giữ lại**: một tham
      // chiếu sẵn có trỏ vào nó phải phân giải được — loại đi thì mở form sửa
      // là ô lookup trống, và lượt lưu kế tiếp xóa lặng lẽ tham chiếu đó.
      const seen = new Set<number>()
      const options: LookupOption[] = []
      for (const row of rows) {
        if (row.is_group || seen.has(row.id)) {
          continue
        }
        seen.add(row.id)
        options.push({ id: row.id, code: row.code, label: row.name })
      }
      return options
    },
  })
}
