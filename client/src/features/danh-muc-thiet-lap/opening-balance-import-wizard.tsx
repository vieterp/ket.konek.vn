/**
 * Wizard nhập Excel số dư ban đầu, 3 bước (FR-OPB-002, cùng khuôn H78/H85 với
 * `ImportWizard` của danh mục — xem tệp đó để đối chiếu). Không dùng chung
 * component với danh mục: wizard kia khóa cứng vào `CatalogDef` và đường dẫn
 * `/api/v1/master/{slug}`, còn ở đây tham số là (năm tài chính, sổ) và báo
 * cáo mang hình `OpeningImportReport` (tổng dư Nợ/Có, `rows_by_kind`,
 * `warnings`) — khác hẳn `ImportReport` của danh mục.
 *
 *   1. Tải tệp mẫu → chọn tệp
 *   2. Kiểm tra dữ liệu — job nền, kết quả là `OpeningImportReport`
 *   3. Nhập khẩu — job nền thứ hai, chỉ mở khi lượt kiểm không còn dòng lỗi
 *      (`error_rows === 0`)
 *
 * Cả hai endpoint `/import/validate` và `/import/commit` được miễn trừ
 * idempotency (RT-12, xem docstring `api/routers/opening_balances.py`): xếp
 * hàng không ghi gì, và lượt ghi thay-trọn-nhóm vốn đã lũy đẳng.
 *
 * Trạng thái wizard nằm trọn trong component và vứt khi đóng — mỗi lần mở là
 * một lượt nhập mới, cùng khuôn `ImportWizard`.
 */

import type { ReactElement } from 'react'
import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { Alert, Button, Drawer } from '@/design-system/components'
import { formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import type { OpeningImportReport } from './use-opening-balances'
import {
  invalidateOpeningBalances,
  isJobRunning,
  openingReportOf,
  useCommitOpeningImport,
  useDownloadOpeningBalanceTemplate,
  useJob,
  useValidateOpeningImport,
} from './use-opening-balances'

export interface OpeningBalanceImportWizardProps {
  readonly fiscalYearId: number
  readonly ledger: number
  readonly open: boolean
  readonly onClose: () => void
}

export function OpeningBalanceImportWizard({
  fiscalYearId,
  ledger,
  open,
  onClose,
}: OpeningBalanceImportWizardProps): ReactElement | null {
  // Dựng lại toàn bộ ruột mỗi lần mở — cùng lý do với `ImportWizard`.
  if (!open) {
    return null
  }
  return <WizardBody fiscalYearId={fiscalYearId} ledger={ledger} onClose={onClose} />
}

function WizardBody({
  fiscalYearId,
  ledger,
  onClose,
}: {
  readonly fiscalYearId: number
  readonly ledger: number
  readonly onClose: () => void
}): ReactElement {
  const { t } = useI18n()
  const { datasetCode } = useSession()
  const queryClient = useQueryClient()

  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [validationJobId, setValidationJobId] = useState<string | null>(null)
  const [commitJobId, setCommitJobId] = useState<string | null>(null)

  const download = useDownloadOpeningBalanceTemplate()
  const validate = useValidateOpeningImport()
  const commit = useCommitOpeningImport()
  const validationJob = useJob(validationJobId)
  const commitJob = useJob(commitJobId)

  const validationReport = openingReportOf(validationJob.data)
  const commitReport = openingReportOf(commitJob.data)

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
    )
  }

  function runValidate(): void {
    if (file === null) {
      setError(t('opening.wizard.error.fileRequired'))
      return
    }
    setError(null)
    setCommitJobId(null)
    validate.mutate(
      { file, fiscalYearId, ledger },
      {
        onSuccess: (response) => {
          setValidationJobId(response.job_id)
        },
        onError: fail,
      },
    )
  }

  function runCommit(): void {
    if (validationJobId === null) {
      return
    }
    setError(null)
    commit.mutate(
      { validationJobId, fiscalYearId, ledger },
      {
        onSuccess: (response) => {
          setCommitJobId(response.job_id)
        },
        onError: fail,
      },
    )
  }

  function finish(): void {
    // Lượt ghi đã đổi dữ liệu — trang đang mở phải nạp lại. Gọi ở MỌI đường
    // đóng một khi job ghi đã được xếp hàng, cùng khuôn `ImportWizard`.
    if (commitJobId !== null) {
      invalidateOpeningBalances(queryClient, datasetCode)
    }
    onClose()
  }

  // Đóng wizard KHI job ghi còn chạy thì `finish()` invalidate quá sớm — nạp
  // lại đúng lúc job xong mới cho danh sách/tổng cân đối số mới (review 4E,
  // M-9). Tín hiệu là job nền (bên ngoài), không phải sự kiện người dùng.
  const commitLanded = commitReport?.committed === true
  useEffect(() => {
    if (commitLanded) {
      invalidateOpeningBalances(queryClient, datasetCode)
    }
  }, [commitLanded, queryClient, datasetCode])

  const validating = validate.isPending || isJobRunning(validationJob.data)
  const committing = commit.isPending || isJobRunning(commitJob.data)
  const canCommit =
    validationReport !== null &&
    validationReport.error_rows === 0 &&
    commitJobId === null &&
    !validating

  const step = commitJobId !== null ? 3 : validationJobId !== null ? 2 : 1

  return (
    <Drawer
      open
      title={t('opening.wizard.title')}
      onClose={finish}
      closeLabel={t('common.close')}
      footer={
        <div className="flex w-full justify-end gap-2">
          <Button variant="secondary" onClick={finish}>
            {t('common.close')}
          </Button>
          {commitReport?.committed === true ? (
            <Button onClick={finish}>{t('opening.wizard.done')}</Button>
          ) : (
            <Button onClick={runCommit} disabled={!canCommit || committing}>
              {committing ? t('opening.wizard.committing') : t('opening.wizard.commit')}
            </Button>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <ol className="flex gap-4 text-xs text-text-muted">
          {[1, 2, 3].map((index) => (
            <li key={index} className={index === step ? 'font-semibold text-primary' : ''}>
              {index}.{' '}
              {t(index === 1 ? 'opening.wizard.step1' : index === 2 ? 'opening.wizard.step2' : 'opening.wizard.step3')}
            </li>
          ))}
        </ol>

        {error !== null && <Alert tone="error">{error}</Alert>}

        <div className="flex flex-col gap-3 rounded border border-border-default p-3">
          <Button
            variant="secondary"
            className="self-start"
            disabled={download.isPending}
            onClick={() => {
              download.mutate()
            }}
          >
            {t('opening.action.downloadTemplate')}
          </Button>
          <p className="text-xs text-text-muted">{t('opening.wizard.templateHint')}</p>

          <label className="flex flex-col gap-1 text-sm font-medium text-text-default">
            {t('opening.wizard.fileLabel')}
            <input
              ref={fileRef}
              type="file"
              accept=".xlsx"
              className="text-sm"
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null)
                setValidationJobId(null)
                setCommitJobId(null)
              }}
            />
          </label>

          <Button className="self-start" onClick={runValidate} disabled={validating}>
            {validating ? t('opening.wizard.validating') : t('opening.wizard.validate')}
          </Button>
        </div>

        {validationJob.data?.status === 'failed' && (
          <Alert tone="error">
            {t('opening.jobFailed', { message: validationJob.data.message ?? '' })}
          </Alert>
        )}

        {validationReport !== null && commitReport === null && (
          <ReportView report={validationReport} committed={false} />
        )}

        {commitJob.data?.status === 'failed' && (
          <Alert tone="error">
            {t('opening.jobFailed', { message: commitJob.data.message ?? '' })}
          </Alert>
        )}

        {/* `committed` lấy từ report thật: lượt ghi kiểm lại toàn bộ và có thể
            từ chối (dữ liệu đổi giữa kiểm ↔ ghi) — job vẫn `done` nhưng
            `committed=false`, không được báo "đã ghi xong" (review 4E, H-2). */}
        {commitReport !== null && !commitReport.committed && (
          <Alert tone="error">{t('opening.wizard.report.commitRejected')}</Alert>
        )}
        {commitReport !== null && (
          <ReportView report={commitReport} committed={commitReport.committed} />
        )}
      </div>
    </Drawer>
  )
}

const DETAIL_KIND_LABEL_KEYS = [
  'opening.detailKind.0',
  'opening.detailKind.1',
  'opening.detailKind.2',
  'opening.detailKind.3',
  'opening.detailKind.4',
] as const

function ReportView({
  report,
  committed,
}: {
  readonly report: OpeningImportReport
  readonly committed: boolean
}): ReactElement {
  const { t, locale } = useI18n()

  const rowsByKind = Object.entries(report.rows_by_kind).filter(([, count]) => count > 0)

  return (
    <section className="flex flex-col gap-2 rounded border border-border-default p-3">
      <h2 className="text-sm font-semibold text-primary">
        {committed ? t('opening.wizard.report.committedTitle') : t('opening.wizard.report.validatedTitle')}
      </h2>
      <p className="text-sm text-text-default">
        {t('opening.wizard.report.summary', {
          total: String(report.total_rows),
          errors: String(report.error_rows),
        })}
      </p>
      {rowsByKind.length > 0 && (
        <p className="text-xs text-text-muted">
          {t('opening.wizard.report.rowsByKind')}{' '}
          {rowsByKind
            .map(([kind, count]) => {
              const labelKey = DETAIL_KIND_LABEL_KEYS[Number(kind)]
              const label = labelKey === undefined ? kind : t(labelKey)
              return `${label}: ${String(count)}`
            })
            .join(' · ')}
        </p>
      )}
      <p className="text-xs text-text-muted">
        {t('opening.wizard.report.totals', {
          debit: formatMoney(report.total_debit, locale),
          credit: formatMoney(report.total_credit, locale),
        })}
      </p>
      {report.warnings.length > 0 && (
        <ul className="list-disc pl-4 text-xs text-status-todo">
          {report.warnings.map((warning, index) => (
            <li key={index}>{warning.message}</li>
          ))}
        </ul>
      )}
      {report.error_rows === 0 && !committed && (
        <Alert tone="info">{t('opening.wizard.report.readyToCommit')}</Alert>
      )}
      {committed && <Alert tone="info">{t('opening.wizard.report.committedBody')}</Alert>}
      {report.errors.length > 0 && (
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-left text-xs">
            <caption className="sr-only">{t('opening.wizard.report.errorsCaption')}</caption>
            <thead>
              <tr className="text-text-muted">
                <th className="py-1 pr-2">{t('opening.wizard.report.errorSheet')}</th>
                <th className="py-1 pr-2">{t('opening.wizard.report.errorRow')}</th>
                <th className="py-1 pr-2">{t('opening.wizard.report.errorColumn')}</th>
                <th className="py-1 pr-2">{t('opening.wizard.report.errorValue')}</th>
                <th className="py-1">{t('opening.wizard.report.errorMessage')}</th>
              </tr>
            </thead>
            <tbody>
              {report.errors.map((rowError, index) => (
                <tr key={index} className="border-t border-border-default">
                  <td className="py-1 pr-2">{rowError.sheet}</td>
                  <td className="py-1 pr-2">{rowError.row}</td>
                  <td className="py-1 pr-2">{rowError.column ?? ''}</td>
                  <td className="py-1 pr-2">{rowError.value ?? ''}</td>
                  <td className="py-1">{rowError.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
