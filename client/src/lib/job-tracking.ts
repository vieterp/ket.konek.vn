/**
 * Theo dõi job nền + lưu tệp về máy — phần dùng chung giữa các nhóm màn hình.
 *
 * Chuyển từ `features/danh-muc-thiet-lap/use-import.ts` ở lát 5E: nhóm Sổ sách
 * & Thuế cũng cần đúng bộ này (job render báo cáo lớn), mà feature không import
 * feature — hạ tầng chung thuộc `lib`. `use-import.ts` re-export lại để mọi
 * chỗ gọi cũ giữ nguyên.
 *
 * Tải tệp đi đường chuẩn trình duyệt (thẻ `<a download>`): không API Tauri
 * trong luồng nghiệp vụ (luật phụ thuộc #6) — mã này phải chạy được ở chế độ
 * trình duyệt LAN v1.x.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type JobResponse = Schemas['JobResponse']

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
    queryFn: () => client.get<JobResponse>(`/api/v1/jobs/${jobId ?? ''}`, { datasetCode }),
    refetchInterval: (query) => (isJobRunning(query.state.data) ? 1000 : false),
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
