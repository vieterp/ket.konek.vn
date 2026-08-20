/**
 * Màn Báo cáo & Sổ sách (lát 5E): danh mục từ server, form tham số theo spec,
 * lưới preview vẽ đúng ô server trả, và đường `202` chuyển-job hiện dải theo
 * dõi với nút Tải tệp khi job xong.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { FakeRoutes, RouteReply } from './feature-test-utils'
import { baseRoutes, mockServer, renderFeatureAt, seedSession } from './feature-test-utils'

const CATALOG: RouteReply = {
  status: 200,
  body: {
    reports: [
      {
        code: 'S03a-DN',
        name: 'Sổ Nhật ký chung',
        name_en: null,
        category: 'Sổ kế toán',
        module: 'reporting',
        ledger_scope: 'both',
      },
      {
        code: 'S06-DN',
        name: 'Bảng cân đối số phát sinh',
        name_en: null,
        category: 'Sổ kế toán',
        module: 'reporting',
        ledger_scope: 'both',
      },
    ],
  },
}

const PARAMS: RouteReply = {
  status: 200,
  body: { code: 'S03a-DN', name: 'Sổ Nhật ký chung', ledger_scope: 'both', params: [] },
}

function routes(extra: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/api/v1/reports': CATALOG,
    '/reports/S03a-DN/params': PARAMS,
    ...extra,
  }
}

describe('màn Báo cáo & Sổ sách', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('danh mục đến từ server; chọn một báo cáo thì hiện form tham số và nút hành động', async () => {
    mockServer(routes())
    renderFeatureAt('/so-sach-thue/bao-cao')

    await userEvent.click(await screen.findByText('Sổ Nhật ký chung'))

    expect(await screen.findByLabelText('Từ ngày')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Xem trước' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Xuất PDF' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Xuất Excel' })).toBeInTheDocument()
    // ledger_scope = both → có ô chọn sổ
    expect(screen.getByText('Tài chính')).toBeInTheDocument()
  })

  it('xem trước vẽ đúng ô server trả — kèm băng cảnh báo khi lưới bị cắt', async () => {
    mockServer(
      routes({
        '/reports/S03a-DN/preview': {
          status: 200,
          body: {
            code: 'S03a-DN',
            name: 'Sổ Nhật ký chung',
            param_lines: ['Từ ngày 01/01/2026 đến ngày 31/12/2026'],
            columns: [
              { key: 'mo_ta', label: 'Diễn giải', label_en: null, type: 'text', align: 'left', width: null },
              { key: 'so_tien', label: 'Số tiền', label_en: null, type: 'money', align: 'right', width: null },
            ],
            rows: [
              { kind: 'group_header', heading: 'Tháng 1', label_span: null, cells: null },
              {
                kind: 'data',
                heading: null,
                label_span: null,
                cells: [
                  { text: 'Thu tiền bán hàng', css: 'cell-text' },
                  { text: '1.000.000', css: 'cell-money cell-right' },
                ],
              },
              {
                kind: 'grand_total',
                heading: null,
                label_span: 1,
                cells: [
                  { text: 'Tổng cộng', css: 'cell-text' },
                  { text: '1.000.000', css: 'cell-money cell-right' },
                ],
              },
            ],
            truncated: true,
          },
        },
      }),
    )
    renderFeatureAt('/so-sach-thue/bao-cao')

    await userEvent.click(await screen.findByText('Sổ Nhật ký chung'))
    await userEvent.click(await screen.findByRole('button', { name: 'Xem trước' }))

    expect(await screen.findByText('Thu tiền bán hàng')).toBeInTheDocument()
    expect(screen.getByText('Tháng 1')).toBeInTheDocument()
    expect(screen.getByText('Tổng cộng')).toBeInTheDocument()
    expect(
      screen.getByText('Lưới xem trước cắt ở 2.000 dòng — xuất Excel để lấy đủ dữ liệu.'),
    ).toBeInTheDocument()
  })

  it('xuất vượt ngưỡng nhận 202: dải job hiện tiến độ rồi nút Tải tệp khi xong', async () => {
    let jobPolls = 0
    mockServer(
      routes({
        '/reports/S03a-DN/render': { status: 202, body: { job_id: 'j-1', estimated_rows: 50000 } },
        '/jobs/j-1': () => {
          jobPolls += 1
          return {
            status: 200,
            body: {
              id: 'j-1',
              type: 'reporting.report.render',
              status: jobPolls === 1 ? 'running' : 'done',
              progress: jobPolls === 1 ? 40 : 100,
              message: null,
              result: null,
              created_at: '2026-08-20T00:00:00Z',
              cancel_requested: false,
            },
          }
        },
      }),
    )
    renderFeatureAt('/so-sach-thue/bao-cao')

    await userEvent.click(await screen.findByText('Sổ Nhật ký chung'))
    await userEvent.click(await screen.findByRole('button', { name: 'Xuất PDF' }))

    // Lượt hỏi đầu: đang chạy — có nút Hủy.
    expect(await screen.findByText(/40%/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hủy' })).toBeInTheDocument()

    // Lượt hỏi kế (nhịp 1s): job xong — nút Tải tệp thay chỗ.
    await waitFor(
      () => {
        expect(screen.getByRole('button', { name: 'Tải tệp' })).toBeInTheDocument()
      },
      { timeout: 3000 },
    )
    expect(screen.getByText('Báo cáo đã sẵn sàng.')).toBeInTheDocument()
  })
})
