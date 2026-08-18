/**
 * Truy vấn và lệnh ghi cho màn hình danh mục dùng chung.
 *
 * Mọi khóa truy vấn đều mang `datasetCode`: cùng một người mở hai bộ sổ sẽ thấy
 * hai bộ danh mục khác nhau, và bộ nhớ đệm không được phép lẫn chúng (cùng lý
 * do với `useAccess`). Lệnh ghi xong thì xóa đệm **cả danh mục** thay vì vá
 * từng trang: danh sách phân trang theo `parent_id`/`flag`/`offset`, và một bản
 * ghi mới có thể xuất hiện ở nhiều tổ hợp tham số hơn là chỗ vừa ghi.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { useSession } from '@/lib/session'

import type { CatalogPage, CatalogRow } from './catalog-types'

const PAGE_SIZE = 100

export interface CatalogListParams {
  readonly parentId?: number | null
  readonly subtreeOf?: number
  readonly flag?: string | null
  readonly page?: number
}

function listPath(slug: string, params: CatalogListParams): string {
  const query = new URLSearchParams()
  if (params.subtreeOf !== undefined) {
    query.set('subtree_of', String(params.subtreeOf))
  } else if (params.parentId !== undefined && params.parentId !== null) {
    query.set('parent_id', String(params.parentId))
  }
  if (params.flag) {
    query.set('flag', params.flag)
  }
  const page = params.page ?? 0
  query.set('limit', String(PAGE_SIZE))
  query.set('offset', String(page * PAGE_SIZE))
  return `/api/v1/master/${slug}?${query.toString()}`
}

export { PAGE_SIZE }

export function useCatalogPage(slug: string, params: CatalogListParams) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['catalog', datasetCode, slug, params],
    enabled: datasetCode !== null,
    queryFn: () => client.get<CatalogPage>(listPath(slug, params), { datasetCode }),
  })
}

export function useCatalogRecord(slug: string, id: number | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['catalog', datasetCode, slug, 'record', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<CatalogRow>(`/api/v1/master/${slug}/${String(id)}`, { datasetCode }),
  })
}

/** Thân lệnh ghi — trường chung + trường riêng của danh mục, đều đã có kiểu ở nơi dựng. */
export type CatalogWriteBody = Readonly<Record<string, unknown>>

export function useCreateCatalogRecord(slug: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
    }: {
      readonly body: CatalogWriteBody
      readonly idempotencyKey: string
    }) => client.post<CatalogRow>(`/api/v1/master/${slug}`, body, { datasetCode, idempotencyKey }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['catalog', datasetCode, slug] })
    },
  })
}

export function useUpdateCatalogRecord(slug: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ id, body }: { readonly id: number; readonly body: CatalogWriteBody }) =>
      client.put<CatalogRow>(`/api/v1/master/${slug}/${String(id)}`, body, { datasetCode }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['catalog', datasetCode, slug] })
    },
  })
}

/** Xóa thật — server chỉ nhận khi chưa ai dùng (BR-SYS-02); đã dùng thì `PUT is_active=false`. */
export function useDeleteCatalogRecord(slug: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (id: number) =>
      client.delete<void>(`/api/v1/master/${slug}/${String(id)}`, { datasetCode }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['catalog', datasetCode, slug] })
    },
  })
}
