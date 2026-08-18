/**
 * Wizard nhập Excel 3 bước (SRS 01 §10, FR-SYS-080..083):
 *
 *   1. Tải tệp mẫu → chọn tệp + hai tùy chọn (chế độ ghi, tham chiếu thiếu)
 *   2. Kiểm tra dữ liệu — job nền, kết quả là `ImportReport` trong `jobs.result`
 *   3. Nhập khẩu — job nền thứ hai, chỉ mở khi lượt kiểm **không còn dòng lỗi**
 *      (FR-SYS-081: bước kiểm là bắt buộc, không nhảy cóc được)
 *
 * Server tự kiểm lại toàn bộ lúc ghi (H85) và so `content_hash`, nên nút "Nhập
 * khẩu" mở theo report chỉ là phép lịch sự với người dùng — cửa thật ở server.
 *
 * Trạng thái wizard nằm trọn trong component và **vứt khi đóng**: mỗi lần mở là
 * một lượt nhập mới với khóa chống trùng mới (RT-12).
 */

import type { ReactElement } from 'react'
import { useRef, useState } from 'react'

import { Alert, Button, Drawer, SelectField } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { useQueryClient } from '@tanstack/react-query'

import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import type { CatalogDef } from './catalog-types'
import { catalogBySlug } from './catalog-registry'
import type { ImportReport } from './use-import'
import {
  importReportOf,
  isJobRunning,
  useCommitImport,
  useDownloadTemplate,
  useJob,
  useValidateImport,
} from './use-import'

export interface ImportWizardProps {
  readonly catalog: CatalogDef
  readonly open: boolean
  readonly onClose: () => void
}

export function ImportWizard({ catalog, open, onClose }: ImportWizardProps): ReactElement | null {
  // Dựng lại toàn bộ ruột mỗi lần mở — cách rẻ nhất để không rơi rớt state cũ.
  if (!open) {
    return null
  }
  return <WizardBody catalog={catalog} onClose={onClose} />
}

function WizardBody({
  catalog,
  onClose,
}: {
  readonly catalog: CatalogDef
  readonly onClose: () => void
}): ReactElement {
  const { t } = useI18n()
  const { datasetCode } = useSession()
  const queryClient = useQueryClient()

  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [mode, setMode] = useState('create_only')
  const [missingReference, setMissingReference] = useState('error')
  const [error, setError] = useState<string | null>(null)
  const [validationJobId, setValidationJobId] = useState<string | null>(null)
  const [commitJobId, setCommitJobId] = useState<string | null>(null)

  // Một khóa cho một lượt mở wizard (RT-12) — component dựng lại mỗi lần mở.
  const [idempotencyKey] = useState(newIdempotencyKey)

  const download = useDownloadTemplate(catalog.slug)
  const validate = useValidateImport(catalog.slug)
  const commit = useCommitImport(catalog.slug)
  const validationJob = useJob(validationJobId)
  const commitJob = useJob(commitJobId)

  const validationReport = importReportOf(validationJob.data)
  const commitReport = importReportOf(commitJob.data)

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : t('error.transport.unreachable'),
    )
  }

  function runValidate(): void {
    if (file === null) {
      setError(t('import.error.fileRequired'))
      return
    }
    setError(null)
    setCommitJobId(null)
    validate.mutate(
      { file, mode, missingReference },
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
      { validationJobId, idempotencyKey },
      {
        onSuccess: (response) => {
          setCommitJobId(response.job_id)
        },
        onError: fail,
      },
    )
  }

  function finish(): void {
    // Lượt ghi đã đổi dữ liệu — mọi trang danh mục đang đệm phải nạp lại. Gọi
    // ở MỌI đường đóng (nút Hoàn tất, nút Đóng, Esc) một khi job ghi đã được
    // gửi đi: đóng bằng Esc sau khi ghi xong mà danh sách vẫn cũ là review 3D
    // M-2.
    if (commitJobId !== null) {
      void queryClient.invalidateQueries({ queryKey: ['catalog', datasetCode, catalog.slug] })
    }
    onClose()
  }

  const validating = validate.isPending || isJobRunning(validationJob.data)
  const committing = commit.isPending || isJobRunning(commitJob.data)
  const canCommit =
    validationReport !== null &&
    validationReport.error_rows === 0 &&
    commitJobId === null &&
    !validating

  // Bước hiện tại chỉ để đánh dấu tiến trình trên đầu wizard.
  const step = commitJobId !== null ? 3 : validationJobId !== null ? 2 : 1

  return (
    <Drawer
      open
      title={t('import.title', { catalog: t(catalog.titleKey) })}
      onClose={finish}
      closeLabel={t('common.close')}
      footer={
        <div className="flex w-full justify-end gap-2">
          <Button variant="secondary" onClick={finish}>
            {t('common.close')}
          </Button>
          {commitReport?.committed === true ? (
            <Button onClick={finish}>{t('import.done')}</Button>
          ) : (
            <Button onClick={runCommit} disabled={!canCommit || committing}>
              {committing ? t('import.committing') : t('import.commit')}
            </Button>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <ol className="flex gap-4 text-xs text-text-muted">
          {[1, 2, 3].map((index) => (
            <li key={index} className={index === step ? 'font-semibold text-primary' : ''}>
              {index}. {t(index === 1 ? 'import.step1' : index === 2 ? 'import.step2' : 'import.step3')}
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
            {t('catalog.action.downloadTemplate')}
          </Button>
          <p className="text-xs text-text-muted">{t('import.templateHint')}</p>

          <label className="flex flex-col gap-1 text-sm font-medium text-text-default">
            {t('import.fileLabel')}
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

          <SelectField
            label={t('import.modeLabel')}
            value={mode}
            onChange={(event) => {
              setMode(event.target.value)
              setValidationJobId(null)
            }}
            options={[
              { value: 'create_only', label: t('import.mode.createOnly') },
              { value: 'create_and_update', label: t('import.mode.createAndUpdate') },
            ]}
          />
          <SelectField
            label={t('import.missingReferenceLabel')}
            value={missingReference}
            onChange={(event) => {
              setMissingReference(event.target.value)
              setValidationJobId(null)
            }}
            options={[
              { value: 'error', label: t('import.missingReference.error') },
              { value: 'create', label: t('import.missingReference.create') },
            ]}
          />

          <Button className="self-start" onClick={runValidate} disabled={validating}>
            {validating ? t('import.validating') : t('import.validate')}
          </Button>
        </div>

        {validationJob.data?.status === 'failed' && (
          <Alert tone="error">
            {t('import.jobFailed', { message: validationJob.data.message ?? '' })}
          </Alert>
        )}

        {validationReport !== null && commitReport === null && (
          <ReportView report={validationReport} committed={false} />
        )}

        {commitJob.data?.status === 'failed' && (
          <Alert tone="error">
            {t('import.jobFailed', { message: commitJob.data.message ?? '' })}
          </Alert>
        )}

        {commitReport !== null && <ReportView report={commitReport} committed />}
      </div>
    </Drawer>
  )
}

function ReportView({
  report,
  committed,
}: {
  readonly report: ImportReport
  readonly committed: boolean
}): ReactElement {
  const { t } = useI18n()

  const createdReferences = Object.entries(report.created_references)

  return (
    <section className="flex flex-col gap-2 rounded border border-border-default p-3">
      <h2 className="text-sm font-semibold text-primary">
        {committed ? t('import.report.committedTitle') : t('import.report.validatedTitle')}
      </h2>
      <p className="text-sm text-text-default">
        {t('import.report.summary', {
          total: String(report.total_rows),
          create: String(report.create_rows),
          update: String(report.update_rows),
          errors: String(report.error_rows),
        })}
      </p>
      {createdReferences.length > 0 && (
        <p className="text-xs text-text-muted">
          {t('import.report.createdReferences')}{' '}
          {createdReferences
            .map(([slug, count]) => {
              const target = catalogBySlug(slug)
              const label = target === undefined ? slug : t(target.titleKey)
              return `${label}: ${String(count)}`
            })
            .join(' · ')}
        </p>
      )}
      {report.error_rows === 0 && !committed && (
        <Alert tone="info">{t('import.report.readyToCommit')}</Alert>
      )}
      {committed && <Alert tone="info">{t('import.report.committedBody')}</Alert>}
      {report.errors.length > 0 && (
        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-left text-xs">
            <caption className="sr-only">{t('import.report.errorsCaption')}</caption>
            <thead>
              <tr className="text-text-muted">
                <th className="py-1 pr-2">{t('import.report.errorRow')}</th>
                <th className="py-1 pr-2">{t('import.report.errorColumn')}</th>
                <th className="py-1 pr-2">{t('import.report.errorValue')}</th>
                <th className="py-1">{t('import.report.errorMessage')}</th>
              </tr>
            </thead>
            <tbody>
              {report.errors.map((rowError, index) => (
                <tr key={index} className="border-t border-border-default">
                  <td className="py-1 pr-2">{rowError.row}</td>
                  <td className="py-1 pr-2">{rowError.column ?? ''}</td>
                  <td className="py-1 pr-2">{rowError.value ?? ''}</td>
                  <td className="py-1">{rowError.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {report.truncated_errors && (
            <p className="mt-1 text-xs text-text-muted">{t('import.report.truncated')}</p>
          )}
        </div>
      )}
    </section>
  )
}
