/**
 * Wizard nhập Excel 3 bước — kiểm cả hai kết cục của bước kiểm: có lỗi (nút
 * Nhập khẩu đóng, bảng lỗi chỉ đúng dòng/cột) và sạch (đi tiếp tới lượt ghi).
 *
 * Job nền giả lập trả `done` ngay từ lượt hỏi đầu để test không phải chờ nhịp
 * polling; đường "job còn chạy" đã nằm trong hợp đồng `refetchInterval` của
 * `useJob` và không cần đồng hồ thật để chứng minh.
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  EMPTY_PAGE,
  baseRoutes,
  mockServer,
  renderFeatureAt,
  seedSession,
} from './feature-test-utils'

function importReport(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    catalog: 'units_of_measure',
    mode: 'create_only',
    missing_reference: 'error',
    file_name: 'don-vi-tinh.xlsx',
    content_hash: 'abc123',
    total_rows: 3,
    create_rows: 2,
    update_rows: 0,
    created_references: {},
    error_rows: 0,
    errors: [],
    truncated_errors: false,
    committed: false,
    ...overrides,
  }
}

function jobDone(result: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 'job-1',
    type: 'master.import.validate',
    status: 'done',
    progress: 100,
    message: null,
    attempt: 1,
    branch_id: null,
    cancel_requested: false,
    requested_by: 1,
    created_at: '2026-08-19T00:00:00Z',
    started_at: '2026-08-19T00:00:00Z',
    finished_at: '2026-08-19T00:00:01Z',
    resume_semantics: 'restart',
    result,
  }
}

async function openWizardAndValidate(): Promise<void> {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Nhập Excel' }))
  const fileInput = await screen.findByLabelText('Tệp Excel (.xlsx)')
  await user.upload(
    fileInput,
    new File(['noi dung'], 'don-vi-tinh.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }),
  )
  await user.click(screen.getByRole('button', { name: 'Kiểm tra dữ liệu' }))
}

describe('wizard nhập Excel', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('tệp có lỗi: bảng lỗi chỉ đúng dòng và cột, nút Nhập khẩu bị đóng (FR-SYS-081)', async () => {
    mockServer({
      ...baseRoutes(),
      '/master/units_of_measure': EMPTY_PAGE,
      '/master/units_of_measure/import/validate': {
        status: 202,
        body: {
          job_id: 'job-1',
          catalog: 'units_of_measure',
          mode: 'create_only',
          missing_reference: 'error',
          file_name: 'don-vi-tinh.xlsx',
          content_hash: 'abc123',
          allow_create_in: [],
        },
      },
      '/jobs/job-1': {
        status: 200,
        body: jobDone(
          importReport({
            error_rows: 1,
            create_rows: 0,
            errors: [
              {
                row: 4,
                column: 'Mã *',
                code: 'data.duplicate',
                message: 'Mã CAI đã tồn tại',
                value: 'CAI',
              },
            ],
          }),
        ),
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/don-vi-tinh')
    await openWizardAndValidate()

    expect(await screen.findByText('Mã CAI đã tồn tại')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('Mã *')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Nhập khẩu' })).toBeDisabled()
  })

  it('tệp sạch: nhập khẩu chạy job thứ hai và báo đã ghi', async () => {
    let commitRequested = false
    mockServer({
      ...baseRoutes(),
      '/master/units_of_measure': EMPTY_PAGE,
      '/master/units_of_measure/import/validate': {
        status: 202,
        body: {
          job_id: 'job-1',
          catalog: 'units_of_measure',
          mode: 'create_only',
          missing_reference: 'error',
          file_name: 'don-vi-tinh.xlsx',
          content_hash: 'abc123',
          allow_create_in: [],
        },
      },
      '/master/units_of_measure/import/commit': (init) => {
        commitRequested = init?.method === 'POST'
        return { status: 202, body: { job_id: 'job-2', catalog: 'units_of_measure' } }
      },
      '/jobs/job-1': { status: 200, body: jobDone(importReport({})) },
      '/jobs/job-2': {
        status: 200,
        body: {
          ...jobDone(importReport({ committed: true, create_rows: 2 })),
          id: 'job-2',
          type: 'master.import.commit',
        },
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/don-vi-tinh')
    await openWizardAndValidate()

    expect(await screen.findByText('Dữ liệu hợp lệ. Bấm "Nhập khẩu" để ghi.')).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Nhập khẩu' }))

    expect(
      await screen.findByText('Đã ghi xong. Đối chiếu lại tại danh sách danh mục.'),
    ).toBeInTheDocument()
    expect(commitRequested).toBe(true)
    expect(screen.getByRole('button', { name: 'Hoàn tất' })).toBeInTheDocument()
  })
})
