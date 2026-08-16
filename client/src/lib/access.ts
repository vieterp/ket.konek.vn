/**
 * Quyền hiệu lực của người đang đăng nhập, **trong dữ liệu kế toán đang mở**.
 *
 * Cùng một người có vai trò khác nhau ở mỗi bộ sổ (2B-1b), nên câu trả lời phụ
 * thuộc `X-Dataset` — và vì thế khóa truy vấn phải mang mã dataset. Thiếu nó
 * thì đổi doanh nghiệp xong màn hình vẫn hiện menu của doanh nghiệp cũ cho tới
 * khi bộ nhớ đệm hết hạn.
 *
 * Dùng để **ẩn** menu và nút, không để quyết định cho phép: mọi lần kiểm thật
 * nằm ở server. Client chỉ tránh mời người dùng bấm vào thứ chắc chắn bị từ
 * chối.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type Access = Schemas['AccessResponse']
export type DatasetSummary = Schemas['DatasetSummary']

export function useAccess() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['access', datasetCode],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<Access>('/api/v1/system/access', { datasetCode }),
  })
}

export function useDatasets() {
  const { client } = useSession()

  return useQuery({
    queryKey: ['datasets'],
    queryFn: async () => {
      const response = await client.get<Schemas['DatasetListResponse']>('/api/v1/system/datasets')
      return response.items
    },
  })
}
