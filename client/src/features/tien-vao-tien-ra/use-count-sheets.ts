/**
 * Kiểm kê quỹ (`/api/v1/cash-book/count-sheets`, FR-QUY-030/031): danh sách,
 * lập biên bản, sinh phiếu xử lý chênh lệch, in biên bản 08a-TT.
 *
 * In biên bản đi qua router của MODULE (không phải `/vouchers/{id}/print`):
 * biên bản không phải chứng từ — không `print_log`, quyền là `count_sheet.view`
 * (quyết định 6E-2).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { saveBlob } from '@/lib/job-tracking'
import { useSession } from '@/lib/session'

export type CountSheet = Schemas['CountSheetOut']
export type CountSheetIn = Schemas['CountSheetIn']

export const COUNT_SHEET_PAGE_SIZE = 50

export function useCountSheets(page: number) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['count-sheets', datasetCode, page],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<Schemas['CountSheetListResponse']>(
        `/api/v1/cash-book/count-sheets?page=${String(page)}&page_size=${String(COUNT_SHEET_PAGE_SIZE)}`,
        { datasetCode },
      ),
  })
}

export function useCreateCountSheet() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
    }: {
      readonly body: CountSheetIn
      readonly idempotencyKey: string
    }) =>
      client.post<CountSheet>(
        '/api/v1/cash-book/count-sheets',
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['count-sheets', datasetCode] })
    },
  })
}

export function useCreateCountSheetAdjustment() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      sheetId,
      idempotencyKey,
      acknowledgeWarnings,
    }: {
      readonly sheetId: string
      readonly idempotencyKey: string
      readonly acknowledgeWarnings?: boolean
    }) =>
      client.post<CountSheet>(
        `/api/v1/cash-book/count-sheets/${sheetId}/actions/create-adjustment` +
          (acknowledgeWarnings === true ? '?acknowledge_warnings=true' : ''),
        undefined,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['count-sheets', datasetCode] })
      void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
    },
  })
}

export function usePrintCountSheet() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async ({
      sheetId,
      templateCode,
    }: {
      readonly sheetId: string
      readonly templateCode: string | null
    }): Promise<void> => {
      const suffix = templateCode === null ? '' : `?template_code=${encodeURIComponent(templateCode)}`
      const outcome = await client.postBlob(
        `/api/v1/cash-book/count-sheets/${sheetId}/print${suffix}`,
        {},
        { datasetCode },
      )
      if (outcome.kind !== 'file') {
        throw new Error('print trả về JSON — hợp đồng chỉ có tệp PDF')
      }
      saveBlob(outcome.blob, outcome.fileName ?? 'bien-ban-kiem-ke-quy.pdf')
    },
  })
}
