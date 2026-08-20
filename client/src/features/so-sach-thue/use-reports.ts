/**
 * Móc dữ liệu của màn Báo cáo & Sổ sách (lát 5E): danh mục báo cáo, spec tham
 * số, preview lưới, và kết xuất PDF/XLSX.
 *
 * Kết xuất có HAI kết cục hợp lệ (hợp đồng `POST /reports/{code}/render`):
 * tệp `200` cho báo cáo nhỏ, hoặc `202` + `{job_id}` khi vượt ngưỡng
 * chuyển-job — khi đó UI theo dõi job qua `useJob` (`@/lib/job-tracking`) rồi
 * tải tệp ở `GET /reports/render-jobs/{job_id}/file`. Client không quyết định
 * đường nào — server đếm dòng và chọn (FR-NFR-041/042/044).
 */

import { useMutation, useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { saveBlob } from '@/lib/job-tracking'
import { useSession } from '@/lib/session'

export type ReportSummary = Schemas['ReportSummaryResponse']
export type ReportParams = Schemas['ReportParamsResponse']
export type ReportParamField = Schemas['ReportParamFieldResponse']
export type ReportPreview = Schemas['ReportPreviewResponse']
export type PreviewRow = Schemas['PreviewRowResponse']
export type PreviewColumn = Schemas['PreviewColumnResponse']
export type RenderAccepted = Schemas['ReportRenderAcceptedResponse']

export type RenderFormat = 'pdf' | 'xlsx'

/** Giá trị tham số người dùng nhập — JSON tự do ở ranh giới, server kiểm. */
export type ReportParamValues = Readonly<Record<string, string | number | boolean>>

export function useReportCatalog() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['reports', datasetCode, 'catalog'],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<Schemas['ReportListResponse']>('/api/v1/reports', { datasetCode }),
  })
}

export function useReportParams(code: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['reports', datasetCode, 'params', code],
    enabled: datasetCode !== null && code !== null,
    queryFn: () =>
      client.get<ReportParams>(`/api/v1/reports/${code ?? ''}/params`, { datasetCode }),
  })
}

export function usePreviewReport() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({ code, params }: { readonly code: string; readonly params: ReportParamValues }) =>
      client.post<ReportPreview>(`/api/v1/reports/${code}/preview`, { params }, { datasetCode }),
  })
}

export type RenderOutcome =
  | { readonly kind: 'downloaded'; readonly fileName: string | null }
  | { readonly kind: 'queued'; readonly accepted: RenderAccepted }

/**
 * Kết xuất một báo cáo. Tệp trả về được lưu ngay xuống máy; `202` trả lại
 * `job_id` để màn hình chuyển sang chế độ theo dõi job.
 */
export function useRenderReport() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async ({
      code,
      format,
      params,
    }: {
      readonly code: string
      readonly format: RenderFormat
      readonly params: ReportParamValues
    }): Promise<RenderOutcome> => {
      const outcome = await client.postBlob(
        `/api/v1/reports/${code}/render`,
        { format, params },
        { datasetCode },
      )
      if (outcome.kind === 'json') {
        return { kind: 'queued', accepted: outcome.data as RenderAccepted }
      }
      saveBlob(outcome.blob, outcome.fileName ?? `${code}.${format}`)
      return { kind: 'downloaded', fileName: outcome.fileName }
    },
  })
}

/** Tải tệp kết quả của một job render đã `done`. */
export function useDownloadRenderJobFile() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async ({ jobId }: { readonly jobId: string }) => {
      const { blob, fileName } = await client.getBlob(
        `/api/v1/reports/render-jobs/${jobId}/file`,
        { datasetCode },
      )
      saveBlob(blob, fileName ?? 'bao-cao')
    },
  })
}

/** Yêu cầu hủy một job đang chạy — dừng ở ranh giới lô, không giết giữa chừng. */
export function useCancelJob() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({ jobId }: { readonly jobId: string }) =>
      client.post<Schemas['JobResponse']>(
        `/api/v1/jobs/${jobId}/cancel`,
        undefined,
        { datasetCode },
      ),
  })
}
