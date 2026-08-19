/**
 * Truy vấn + lệnh cho màn hình Số dư ban đầu
 * (`/danh-muc-thiet-lap/so-du-ban-dau`, lát 4E, FR-OPB-*).
 *
 * Job nền dùng chung khuôn H78 với nhập Excel danh mục: xếp hàng trả `job_id`
 * ngay, kết quả nằm trong `jobs.result`. Theo dõi tiến độ dùng lại `useJob` +
 * `saveBlob` của `./use-import` — cùng cơ chế polling, không chép lại vòng
 * lặp `refetchInterval`.
 *
 * `openingReportOf` thì viết riêng, KHÔNG dùng `importReportOf` của
 * `use-import.ts`: báo cáo số dư ban đầu là một model khác hẳn báo cáo nhập
 * danh mục (`OpeningImportReport` phía server, xem
 * `posting/opening_balances/report.py`) — tổng dư Nợ/Có, `rows_by_kind`,
 * không phải `create_rows`/`created_references`. Ép kiểu `importReportOf` trả
 * về sẽ đọc sai tên trường.
 *
 * `useFiscalYears` khai lại (không import từ `features/so-sach-thue`): mỗi
 * nhóm màn hình tự quản dữ liệu của mình (xem ghi chú đầu `index.ts` — thư
 * mục theo NHÓM MÀN HÌNH, không theo module backend), và bản thân hook này
 * chỉ hơn chục dòng.
 *
 * Job `posting.opening_balances.carry_forward`/`clear_group` xếp hàng thẳng
 * qua `POST /api/v1/jobs` — hai loại này KHÔNG có endpoint REST riêng (M5,
 * FR-OPB-010) và được miễn trừ idempotency (RT-12): xếp hàng không đổi trạng
 * thái nghiệp vụ, và thân job đã lũy đẳng theo `resume_semantics`
 * (`api/routers/jobs.py`).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

import { saveBlob } from './use-import'
import type { JobResponse } from './use-import'

export { isJobRunning, saveBlob, useJob } from './use-import'
export type { JobResponse } from './use-import'

export type OpeningBalanceRow = Schemas['OpeningBalanceRowResponse']
export type OpeningBalanceList = Schemas['OpeningBalanceListResponse']
export type OpeningValidateResponse = Schemas['OpeningValidateResponse']
export type OpeningCommitResponse = Schemas['OpeningCommitResponse']
export type FiscalYear = Schemas['FiscalYearResponse']

/** `page` 1-based, khớp `Query(ge=1)` phía server (`opening_balances.py`). */
export const OPENING_BALANCE_PAGE_SIZE = 50

const OPENING_BALANCES_KEY = 'opening-balances'

export function useFiscalYears() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['fiscal-years', datasetCode],
    enabled: datasetCode !== null,
    queryFn: async (): Promise<readonly FiscalYear[]> => {
      const response = await client.get<Schemas['FiscalYearListResponse']>(
        '/api/v1/fiscal-years',
        { datasetCode },
      )
      return response.items
    },
  })
}

export interface OpeningBalanceListParams {
  readonly fiscalYearId: number
  readonly ledger: number
  readonly detailKind: number
  readonly page: number
}

function listPath(params: OpeningBalanceListParams): string {
  const query = new URLSearchParams()
  query.set('fiscal_year_id', String(params.fiscalYearId))
  query.set('ledger', String(params.ledger))
  query.set('detail_kind', String(params.detailKind))
  query.set('page', String(params.page))
  query.set('page_size', String(OPENING_BALANCE_PAGE_SIZE))
  return `/api/v1/opening-balances?${query.toString()}`
}

export function useOpeningBalances(params: OpeningBalanceListParams | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: [OPENING_BALANCES_KEY, datasetCode, params],
    enabled: datasetCode !== null && params !== null,
    queryFn: () =>
      client.get<OpeningBalanceList>(listPath(params as OpeningBalanceListParams), {
        datasetCode,
      }),
  })
}

/** Xóa đệm mọi trang/tab/sổ đang có — một lệnh ghi có thể đổi tổng cân đối
 * hiển thị ở tab khác với tab vừa ghi (hai tổng tính trên cả (năm, sổ)). */
export function invalidateOpeningBalances(
  queryClient: ReturnType<typeof useQueryClient>,
  datasetCode: string | null,
): void {
  void queryClient.invalidateQueries({ queryKey: [OPENING_BALANCES_KEY, datasetCode] })
}

export function useDownloadOpeningBalanceTemplate() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: async () => {
      const { blob, fileName } = await client.getBlob('/api/v1/opening-balances/template', {
        datasetCode,
      })
      saveBlob(blob, fileName ?? 'so-du-ban-dau-mau.xlsx')
    },
  })
}

export function useValidateOpeningImport() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({
      file,
      fiscalYearId,
      ledger,
    }: {
      readonly file: File
      readonly fiscalYearId: number
      readonly ledger: number
    }) => {
      const form = new FormData()
      form.append('file', file)
      form.append('fiscal_year_id', String(fiscalYearId))
      form.append('ledger', String(ledger))
      return client.postForm<OpeningValidateResponse>(
        '/api/v1/opening-balances/import/validate',
        form,
        { datasetCode },
      )
    },
  })
}

export function useCommitOpeningImport() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({
      validationJobId,
      fiscalYearId,
      ledger,
    }: {
      readonly validationJobId: string
      readonly fiscalYearId: number
      readonly ledger: number
    }) =>
      client.post<OpeningCommitResponse>(
        '/api/v1/opening-balances/import/commit',
        { validation_job_id: validationJobId, fiscal_year_id: fiscalYearId, ledger },
        { datasetCode },
      ),
  })
}

/** `OpeningRowError` phía server — thêm chiều `sheet` so với lỗi nhập danh mục. */
export interface OpeningRowError {
  readonly sheet: string
  readonly row: number
  readonly column: string | null
  readonly code: string
  readonly message: string
  readonly value: string | null
}

export interface OpeningImportWarning {
  readonly code: string
  readonly message: string
}

/** `OpeningImportReport` phía server (`posting/opening_balances/report.py`) — một model cho cả kiểm lẫn ghi (H85). */
export interface OpeningImportReport {
  readonly fiscal_year_id: number
  readonly ledger: number
  readonly file_name: string
  readonly content_hash: string
  readonly total_rows: number
  readonly error_rows: number
  readonly errors: readonly OpeningRowError[]
  readonly warnings: readonly OpeningImportWarning[]
  /** `{detail_kind: số dòng}` — khóa là chuỗi số vì JSON không có khóa số. */
  readonly rows_by_kind: Readonly<Record<string, number>>
  readonly invoice_rows: number
  readonly replaced_kinds: readonly number[]
  readonly total_debit: string
  readonly total_credit: string
  readonly committed: boolean
}

/** Đọc `OpeningImportReport` từ `jobs.result` — `null` khi job chưa xong hoặc không có report. */
export function openingReportOf(job: JobResponse | undefined): OpeningImportReport | null {
  const result = job?.result
  if (result === null || result === undefined || typeof result !== 'object') {
    return null
  }
  const candidate = result as Partial<OpeningImportReport>
  if (typeof candidate.total_rows !== 'number' || !Array.isArray(candidate.errors)) {
    return null
  }
  return candidate as OpeningImportReport
}

const CARRY_FORWARD_JOB_TYPE = 'posting.opening_balances.carry_forward'
const CLEAR_GROUP_JOB_TYPE = 'posting.opening_balances.clear_group'

export function useCarryForwardOpeningBalances() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({
      fromFiscalYearId,
      overwrite,
    }: {
      readonly fromFiscalYearId: number
      readonly overwrite: boolean
    }) =>
      client.post<JobResponse>(
        '/api/v1/jobs',
        { type: CARRY_FORWARD_JOB_TYPE, params: { from_fiscal_year_id: fromFiscalYearId, overwrite } },
        { datasetCode },
      ),
  })
}

/** Kết quả `run_carry_forward` (`posting/opening_balances/carry_forward_job.py`). */
export interface CarryForwardResult {
  readonly target_fiscal_year_id: number
  readonly rows_financial: number
  readonly rows_management: number
  readonly invoices_carried: number
  readonly invoices_dropped: number
  readonly invoice_overrun_parents: number
}

export function carryForwardResultOf(job: JobResponse | undefined): CarryForwardResult | null {
  const result = job?.result
  if (result === null || result === undefined || typeof result !== 'object') {
    return null
  }
  const candidate = result as Partial<CarryForwardResult>
  if (typeof candidate.target_fiscal_year_id !== 'number') {
    return null
  }
  return candidate as CarryForwardResult
}

export function useClearOpeningBalanceGroup() {
  const { client, datasetCode } = useSession()

  return useMutation({
    mutationFn: ({
      fiscalYearId,
      ledger,
      detailKind,
    }: {
      readonly fiscalYearId: number
      readonly ledger: number
      readonly detailKind: number
    }) =>
      client.post<JobResponse>(
        '/api/v1/jobs',
        {
          type: CLEAR_GROUP_JOB_TYPE,
          params: { fiscal_year_id: fiscalYearId, ledger, detail_kind: detailKind },
        },
        { datasetCode },
      ),
  })
}

/** Kết quả `run_clear_group` (`posting/opening_balances/clear_job.py`). */
export interface ClearGroupResult {
  readonly fiscal_year_id: number
  readonly ledger: number
  readonly detail_kind: number
  readonly deleted_rows: number
}

export function clearGroupResultOf(job: JobResponse | undefined): ClearGroupResult | null {
  const result = job?.result
  if (result === null || result === undefined || typeof result !== 'object') {
    return null
  }
  const candidate = result as Partial<ClearGroupResult>
  if (typeof candidate.deleted_rows !== 'number') {
    return null
  }
  return candidate as ClearGroupResult
}

/**
 * Trích mã lỗi ổn định từ `job.message` dạng `"[error.code] câu tiếng Việt"`
 * (`worker/runner.py`, `_finalize_failure`). `null` khi job hỏng vì lý do
 * không mang mã ổn định (lỗi hạ tầng, ngoại lệ chưa gói `DomainError`).
 */
export function jobErrorCode(message: string | null | undefined): string | null {
  const match = message?.match(/^\[([\w.]+)\]/)
  return match?.[1] ?? null
}
