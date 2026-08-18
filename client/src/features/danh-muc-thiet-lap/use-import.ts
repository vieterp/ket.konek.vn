/**
 * Đường nhập/xuất Excel của danh mục (FR-SYS-013/014, SRS 01 §10).
 *
 * Cả kiểm lẫn ghi đều là **job nền** (H78): endpoint trả `job_id` ngay, kết quả
 * — chính là `ImportReport` — nằm trong `jobs.result`. Vì thế phần lớn tệp này
 * là chuyện theo dõi một job: hỏi lại theo nhịp khi nó còn chạy, dừng hỏi khi
 * nó xong/hỏng.
 *
 * Tải tệp (mẫu, xuất) đi đường chuẩn trình duyệt (`getBlob` + thẻ `<a
 * download>`): không API Tauri trong luồng nghiệp vụ (luật phụ thuộc #6) — cùng
 * mã này phải chạy được ở chế độ trình duyệt LAN v1.x.
 */

import { useMutation, useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type ImportValidateResponse = Schemas['ImportValidateResponse']
export type ImportCommitResponse = Schemas['ImportCommitResponse']
export type JobResponse = Schemas['JobResponse']

/** `jobs.result` của một job nhập danh mục — model `ImportReport` phía server. */
export interface ImportReport {
  readonly catalog: string
  readonly mode: string
  readonly missing_reference: string
  readonly file_name: string
  readonly content_hash: string
  readonly total_rows: number
  readonly create_rows: number
  readonly update_rows: number
  readonly created_references: Readonly<Record<string, number>>
  readonly error_rows: number
  readonly errors: readonly {
    readonly row: number
    readonly column: string | null
    readonly code: string
    readonly message: string
    readonly value: string | null
  }[]
  readonly truncated_errors: boolean
  readonly committed: boolean
}

const RUNNING_STATUSES: readonly string[] = ['queued', 'running']

export function isJobRunning(job: JobResponse | undefined): boolean {
  return job !== undefined && RUNNING_STATUSES.includes(job.status)
}

/** Theo dõi một job: hỏi lại mỗi giây khi còn chạy, dừng hẳn khi có kết cục. */
export function useJob(jobId: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['job', datasetCode, jobId],
    enabled: datasetCode !== null && jobId !== null,
    queryFn: () =>
      client.get<JobResponse>(`/api/v1/jobs/${jobId ?? ''}`, { datasetCode }),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 1000 : false),
  })
}

/** Đọc `ImportReport` từ `jobs.result` — `null` khi job chưa xong hoặc không có report. */
export function importReportOf(job: JobResponse | undefined): ImportReport | null {
  const result = job?.result
  if (result === null || result === undefined || typeof result !== 'object') {
    return null
  }
  const candidate = result as Partial<ImportReport>
  if (typeof candidate.total_rows !== 'number' || !Array.isArray(candidate.errors)) {
    return null
  }
  return candidate as ImportReport
}

export function useValidateImport(slug: string) {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({
      file,
      mode,
      missingReference,
    }: {
      readonly file: File
      readonly mode: string
      readonly missingReference: string
    }) => {
      const form = new FormData()
      form.append('file', file)
      form.append('mode', mode)
      form.append('missing_reference', missingReference)
      return client.postForm<ImportValidateResponse>(
        `/api/v1/master/${slug}/import/validate`,
        form,
        { datasetCode },
      )
    },
  })
}

export function useCommitImport(slug: string) {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({
      validationJobId,
      idempotencyKey,
    }: {
      readonly validationJobId: string
      readonly idempotencyKey: string
    }) =>
      client.post<ImportCommitResponse>(
        `/api/v1/master/${slug}/import/commit`,
        { validation_job_id: validationJobId },
        { datasetCode, idempotencyKey },
      ),
  })
}

/** Lưu một blob xuống máy bằng thẻ `<a download>` — đường chuẩn trình duyệt. */
export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

/** Tải tệp mẫu Excel của danh mục (FR-SYS-080). */
export function useDownloadTemplate(slug: string) {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async () => {
      const { blob, fileName } = await client.getBlob(`/api/v1/master/${slug}/template`, {
        datasetCode,
      })
      saveBlob(blob, fileName ?? `${slug}-mau.xlsx`)
    },
  })
}

/** Xuất danh mục ra Excel (FR-SYS-014). */
export function useExportCatalog(slug: string) {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async ({ includeInactive }: { readonly includeInactive: boolean }) => {
      const query = includeInactive ? '?include_inactive=true' : ''
      const { blob, fileName } = await client.getBlob(
        `/api/v1/master/${slug}/export${query}`,
        { datasetCode },
      )
      saveBlob(blob, fileName ?? `${slug}.xlsx`)
    },
  })
}
