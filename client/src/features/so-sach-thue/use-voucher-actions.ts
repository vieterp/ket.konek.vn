/**
 * Ba hành vi trên chứng từ đã cất: ghi sổ, bỏ ghi sổ, xóa (SRS 00 §3.3).
 *
 * `post`/`unpost` đòi khóa chống trùng RIÊNG cho từng lượt bấm — chỗ gọi sinh
 * `newIdempotencyKey()` đúng lúc bấm (không sinh trước, không dùng lại): mỗi
 * dòng trong bảng danh sách có thể bấm "Ghi sổ" độc lập, và khóa dùng chung
 * cho nhiều dòng sẽ khiến lượt bấm thứ hai bị coi là lặp lại lượt đầu.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type VoucherResponse = Schemas['VoucherResponse']

interface ActionArgs {
  readonly id: string
  readonly idempotencyKey: string
}

export function useVoucherActions() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  function invalidate(): void {
    void queryClient.invalidateQueries({ queryKey: ['vouchers', datasetCode] })
  }

  const post = useMutation({
    mutationFn: ({ id, idempotencyKey }: ActionArgs) =>
      client.post<VoucherResponse>(`/api/v1/vouchers/${id}/actions/post`, undefined, {
        datasetCode,
        idempotencyKey,
      }),
    onSuccess: invalidate,
  })

  const unpost = useMutation({
    mutationFn: ({ id, idempotencyKey }: ActionArgs) =>
      client.post<VoucherResponse>(`/api/v1/vouchers/${id}/actions/unpost`, undefined, {
        datasetCode,
        idempotencyKey,
      }),
    onSuccess: invalidate,
  })

  const remove = useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/vouchers/${id}`, { datasetCode }),
    onSuccess: invalidate,
  })

  return { post, unpost, remove }
}
