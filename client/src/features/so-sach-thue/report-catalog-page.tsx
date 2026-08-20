/**
 * Màn Báo cáo & Sổ sách (`/so-sach-thue/bao-cao`, lát 5E — FR-RPT-001/002/006).
 *
 * Danh mục bên trái đến từ `GET /reports` — server đã lọc theo chế độ kế toán
 * của dữ liệu (H2 review 5D), client không giữ danh sách mã mẫu nào. Chọn một
 * báo cáo → form tham số dựng từ `GET /params` → Xem trước (lưới) hoặc Xuất
 * PDF/XLSX.
 *
 * Xuất có hai kết cục hợp lệ: tệp tải về ngay, hoặc `202` khi server đếm dòng
 * thấy vượt ngưỡng chuyển-job (bước 19) — khi đó màn hình chuyển sang dải theo
 * dõi job: tiến độ, nút Hủy, nút Tải tệp lúc xong. UI không khóa (FR-NFR-044).
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { isJobRunning, useJob } from '@/lib/job-tracking'
import { ApiError } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import { todayIso } from './local-date'
import { LEDGER_FINANCIAL, ReportParamsPanel } from './report-params-panel'
import { ReportPreviewGrid } from './report-preview-grid'
import type { ReportParamValues, ReportSummary } from './use-reports'
import {
  useCancelJob,
  useDownloadRenderJobFile,
  usePreviewReport,
  useRenderReport,
  useReportCatalog,
  useReportParams,
} from './use-reports'

function yearStartIso(): string {
  return `${todayIso().slice(0, 4)}-01-01`
}

function errorText(
  t: ReturnType<typeof useI18n>['t'],
  error: unknown,
): string | null {
  if (error instanceof ApiError) {
    return translateErrorCode(t, error.errorCode)
  }
  if (error instanceof Error) {
    return t('error.transport.unreachable')
  }
  return null
}

export function ReportCatalogPage(): ReactElement {
  const { t } = useI18n()
  const catalog = useReportCatalog()

  const [selectedCode, setSelectedCode] = useState<string | null>(null)
  const [fromDate, setFromDate] = useState(yearStartIso)
  const [toDate, setToDate] = useState(todayIso)
  const [ledger, setLedger] = useState(LEDGER_FINANCIAL)
  const [extraValues, setExtraValues] = useState<ReportParamValues>({})
  const [activeJobId, setActiveJobId] = useState<string | null>(null)

  const spec = useReportParams(selectedCode)
  const preview = usePreviewReport()
  const render = useRenderReport()
  const downloadJobFile = useDownloadRenderJobFile()
  const cancelJob = useCancelJob()
  const job = useJob(activeJobId)

  const reports = catalog.data?.reports ?? []
  const byCategory = new Map<string, ReportSummary[]>()
  for (const report of reports) {
    const bucket = byCategory.get(report.category) ?? []
    bucket.push(report)
    byCategory.set(report.category, bucket)
  }

  function selectReport(code: string): void {
    setSelectedCode(code)
    setExtraValues({})
    // Reset về sổ tài chính (M-3 review 5E): giá trị "quản trị" còn dính từ
    // báo cáo trước sẽ theo người dùng sang báo cáo kế — bất ngờ khó thấy.
    setLedger(LEDGER_FINANCIAL)
    setActiveJobId(null)
    preview.reset()
    render.reset()
  }

  function currentParams(): ReportParamValues {
    const params: Record<string, string | number | boolean> = {
      from_date: fromDate,
      to_date: toDate,
      ...extraValues,
    }
    // Chỉ gửi `ledger` khi definition cho chọn sổ (M-3 review 5E): báo cáo
    // ghim sổ từ chối tường minh tham số `ledger` lệch scope — gửi luôn nghĩa
    // là 422 vĩnh viễn mà màn hình không có ô nào để sửa.
    if (spec.data?.ledger_scope === 'both') {
      params.ledger = Number(ledger)
    }
    return params
  }

  function exportAs(format: 'pdf' | 'xlsx'): void {
    if (selectedCode === null) {
      return
    }
    // `mutate` + callback thay vì `void mutateAsync` (L-2 review 5E): promise
    // bị bỏ rơi reject thành unhandled rejection dù lỗi đã hiện qua state.
    render.mutate(
      { code: selectedCode, format, params: currentParams() },
      {
        onSuccess: (outcome) => {
          if (outcome.kind === 'queued') {
            setActiveJobId(outcome.accepted.job_id)
          }
        },
      },
    )
  }

  const pageError =
    errorText(t, catalog.error) ??
    errorText(t, spec.error) ??
    errorText(t, preview.error) ??
    errorText(t, render.error) ??
    errorText(t, downloadJobFile.error) ??
    errorText(t, cancelJob.error)

  const jobData = job.data
  const jobRunning = isJobRunning(jobData)

  return (
    <div className="flex h-full gap-4 p-4">
      <FeatureNav />
      <div className="flex min-w-0 flex-1 gap-4">
        <nav
          aria-label={t('reports.catalog.title')}
          className="w-[260px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
        >
          <h2 className="mb-2 text-sm font-semibold text-text-muted">
            {t('reports.catalog.title')}
          </h2>
          {catalog.isLoading ? <p className="text-sm">{t('reports.catalog.loading')}</p> : null}
          {[...byCategory.entries()].map(([category, items]) => (
            <div key={category} className="mb-3">
              <h3 className="mb-1 text-xs font-semibold uppercase text-text-muted">{category}</h3>
              <ul>
                {items.map((report) => (
                  <li key={report.code}>
                    <button
                      type="button"
                      onClick={() => {
                        selectReport(report.code)
                      }}
                      className={`block w-full rounded px-2 py-1 text-left text-sm ${
                        report.code === selectedCode
                          ? 'bg-navy-50 font-semibold text-primary'
                          : 'text-text-default hover:bg-primary/10 hover:text-primary'
                      }`}
                    >
                      <span className="font-mono text-xs text-text-muted">{report.code}</span>{' '}
                      {report.name}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <section className="flex min-w-0 flex-1 flex-col gap-3">
          {pageError !== null ? <Alert tone="error">{pageError}</Alert> : null}
          {selectedCode === null ? (
            <p className="text-sm text-text-muted">{t('reports.catalog.pickOne')}</p>
          ) : spec.data === undefined ? null : (
            <>
              <ReportParamsPanel
                spec={spec.data}
                fromDate={fromDate}
                toDate={toDate}
                ledger={ledger}
                extraValues={extraValues}
                onFromDate={setFromDate}
                onToDate={setToDate}
                onLedger={setLedger}
                onExtraValue={(name, value) => {
                  setExtraValues((current) => ({ ...current, [name]: value }))
                }}
              />
              <div className="flex gap-2">
                <Button
                  onClick={() => {
                    preview.mutate({ code: selectedCode, params: currentParams() })
                  }}
                  disabled={preview.isPending}
                >
                  {t('reports.action.preview')}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    exportAs('pdf')
                  }}
                  disabled={render.isPending}
                >
                  {t('reports.action.exportPdf')}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    exportAs('xlsx')
                  }}
                  disabled={render.isPending}
                >
                  {t('reports.action.exportXlsx')}
                </Button>
              </div>

              {activeJobId !== null && jobData !== undefined ? (
                <Alert tone={jobData.status === 'failed' ? 'error' : 'info'}>
                  <div className="flex items-center gap-3">
                    <span>
                      {jobRunning
                        ? t('reports.job.running', {
                            progress: String(jobData.progress),
                          })
                        : jobData.status === 'done'
                          ? t('reports.job.done')
                          : jobData.status === 'cancelled'
                            ? t('reports.job.cancelled')
                            : t('reports.job.failed')}
                    </span>
                    {jobData.message !== null && jobData.message !== undefined ? (
                      <span className="text-xs text-text-muted">{jobData.message}</span>
                    ) : null}
                    {jobRunning ? (
                      <Button
                        variant="secondary"
                        onClick={() => {
                          cancelJob.mutate({ jobId: activeJobId })
                        }}
                        disabled={cancelJob.isPending}
                      >
                        {t('reports.job.cancel')}
                      </Button>
                    ) : null}
                    {jobData.status === 'done' ? (
                      <Button
                        onClick={() => {
                          downloadJobFile.mutate({ jobId: activeJobId })
                        }}
                        disabled={downloadJobFile.isPending}
                      >
                        {t('reports.job.download')}
                      </Button>
                    ) : null}
                  </div>
                </Alert>
              ) : null}

              {preview.data !== undefined ? <ReportPreviewGrid preview={preview.data} /> : null}
            </>
          )}
        </section>
      </div>
    </div>
  )
}
