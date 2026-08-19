/**
 * CRUD chứng từ nghiệp vụ khác (`/api/v1/gl/journal-vouchers`) — lát 4E.
 *
 * `PUT` không đòi khóa chống trùng (khác `POST`): gửi lại cùng `row_version`
 * thì lần thứ hai đã bị chính khóa lạc quan chặn bằng `409` (hợp đồng nêu
 * trong OpenAPI của server).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type JournalVoucherOut = Schemas['JournalVoucherOut']
export type JournalVoucherIn = Schemas['JournalVoucherIn']
export type JournalVoucherUpdate = Schemas['JournalVoucherUpdate']

export function useJournalVoucher(id: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['gl-journal-voucher', datasetCode, id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<JournalVoucherOut>(`/api/v1/gl/journal-vouchers/${String(id)}`, { datasetCode }),
  })
}

export function useCreateJournalVoucher() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
    }: {
      readonly body: JournalVoucherIn
      readonly idempotencyKey: string
    }) =>
      // `ApiClient.post` nhận thân JSON dạng `Record<string, unknown>` cho MỌI
      // loại request; `JournalVoucherIn` không tự có index signature nên phải
      // ép kiểu ở đúng một dòng — bản thân `body` đã được kiểm kiểu đầy đủ ở
      // nơi dựng nó (`resolveLines` + các trường header).
      client.post<JournalVoucherOut>(
        '/api/v1/gl/journal-vouchers',
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['vouchers', datasetCode] })
    },
  })
}

export function useUpdateJournalVoucher(id: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (body: JournalVoucherUpdate) =>
      client.put<JournalVoucherOut>(
        `/api/v1/gl/journal-vouchers/${id}`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
    onSuccess: (voucher) => {
      void queryClient.invalidateQueries({ queryKey: ['vouchers', datasetCode] })
      queryClient.setQueryData(['gl-journal-voucher', datasetCode, voucher.id], voucher)
    },
  })
}
