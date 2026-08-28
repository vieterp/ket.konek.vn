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
  /**
   * Lượt gửi lại SAU khi người dùng bấm "Vẫn ghi sổ?" (FR-SYS-062 mức "Cảnh
   * báo") — chỉ truyền `true` khi lượt trước bị từ chối mà mọi vi phạm đều
   * mang `details.warning` (xem `journal-violations.allAcknowledgeableWarnings`).
   */
  readonly acknowledgeWarnings?: boolean
}

export function useVoucherActions() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  function invalidate(): void {
    void queryClient.invalidateQueries({ queryKey: ['vouchers', datasetCode] })
    // Hook dùng chung cho cả chứng từ GLE lẫn phiếu quỹ/ngân hàng — ghi sổ /
    // bỏ ghi sổ đổi số dư thẻ và lưới của màn "Tiền vào tiền ra".
    void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
  }

  const post = useMutation({
    mutationFn: ({ id, idempotencyKey, acknowledgeWarnings }: ActionArgs) =>
      client.post<VoucherResponse>(
        `/api/v1/vouchers/${id}/actions/post${acknowledgeWarnings === true ? '?acknowledge_warnings=true' : ''}`,
        undefined,
        { datasetCode, idempotencyKey },
      ),
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
