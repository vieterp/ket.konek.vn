/**
 * Bảng danh sách — kiểm ba bất biến mà một phần mềm kế toán không được sai.
 *
 * 1. Cột số căn phải (đọc lệch cột tiền là lỗi nghiệp vụ, không phải lỗi đẹp/xấu)
 * 2. Sắp xếp do chỗ gọi làm — component KHÔNG tự đảo thứ tự dòng
 * 3. Bảng rỗng nói rõ là rỗng, không im lặng
 */

import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { DataTableColumn, DataTableTotalRow } from './data-table'
import { DataTable } from './data-table'

interface Row {
  readonly id: string
  readonly code: string
  readonly amount: string
}

const ROWS: readonly Row[] = [
  { id: '1', code: 'PC-001', amount: '1.500.000' },
  { id: '2', code: 'PC-002', amount: '250.000' },
]

const COLUMNS: readonly DataTableColumn<Row>[] = [
  { key: 'code', header: 'Số chứng từ', sortable: true, render: (row) => row.code },
  { key: 'amount', header: 'Số tiền', align: 'right', sortable: true, render: (row) => row.amount },
]

function renderTable(props: Partial<Parameters<typeof DataTable<Row>>[0]> = {}) {
  return render(
    <DataTable
      caption="Phiếu chi trong kỳ"
      columns={COLUMNS}
      rows={ROWS}
      rowKey={(row) => row.id}
      emptyLabel="Chưa có chứng từ nào."
      {...props}
    />,
  )
}

describe('DataTable', () => {
  it('căn phải và dùng tabular-nums cho cột khai align="right"', () => {
    renderTable()

    const amountCell = screen.getByText('1.500.000')
    expect(amountCell).toHaveClass('text-right')
    // Font tỉ lệ làm chữ số rộng hẹp khác nhau → các cột số lệch nhau khi đọc dọc.
    expect(amountCell).toHaveClass('tabular-nums')

    expect(screen.getByText('PC-001')).toHaveClass('text-left')
  })

  it('KHÔNG tự sắp xếp dòng — chỉ báo cột nào vừa được bấm', async () => {
    const user = userEvent.setup()
    const onSortChange = vi.fn()
    renderTable({ onSortChange })

    await user.click(screen.getByRole('button', { name: /Số tiền/ }))

    expect(onSortChange).toHaveBeenCalledExactlyOnceWith({ key: 'amount', direction: 'asc' })
    // Thứ tự dòng giữ nguyên: danh sách thật lấy theo trang từ server, sắp xếp
    // tại chỗ chỉ sắp đúng trang đang xem.
    const rows = screen.getAllByRole('row').slice(1)
    expect(within(rows[0] as HTMLElement).getByText('PC-001')).toBeInTheDocument()
  })

  it('bấm lại cột đang sắp thì đảo chiều', async () => {
    const user = userEvent.setup()
    const onSortChange = vi.fn()
    renderTable({ onSortChange, sort: { key: 'code', direction: 'asc' } })

    await user.click(screen.getByRole('button', { name: /Số chứng từ/ }))

    expect(onSortChange).toHaveBeenCalledExactlyOnceWith({ key: 'code', direction: 'desc' })
  })

  it('đặt aria-sort ĐÚNG một cột — cột đang sắp', () => {
    renderTable({ onSortChange: vi.fn(), sort: { key: 'amount', direction: 'desc' } })

    const headers = screen.getAllByRole('columnheader')
    expect(headers[0]).not.toHaveAttribute('aria-sort')
    expect(headers[1]).toHaveAttribute('aria-sort', 'descending')
  })

  it('không cho bấm sắp xếp khi chỗ gọi không nhận onSortChange', () => {
    renderTable()

    // Một nút bấm không làm gì còn tệ hơn không có nút.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('bảng rỗng nói rõ là rỗng', () => {
    renderTable({ rows: [] })

    expect(screen.getByText('Chưa có chứng từ nào.')).toBeInTheDocument()
  })

  it('đang tải thì hiện nhãn đang tải chứ không hiện "chưa có gì"', () => {
    renderTable({ rows: [], loading: true, loadingLabel: 'Đang tải…' })

    // Hai trạng thái này khác nhau với người dùng: một cái là "chờ", cái kia là
    // "đi nhập liệu đi". Lẫn lộn thì người dùng bỏ đi nhập lại thứ đã có.
    expect(screen.getByText('Đang tải…')).toBeInTheDocument()
    expect(screen.queryByText('Chưa có chứng từ nào.')).not.toBeInTheDocument()
  })

  it('vẫn hiện nhãn đang tải khi đã có dòng cũ trên màn hình', () => {
    renderTable({ loading: true, loadingLabel: 'Đang tải…' })

    expect(screen.getByText('Đang tải…')).toBeInTheDocument()
    expect(screen.queryByText('PC-001')).not.toBeInTheDocument()
  })

  it('dòng tổng hiện đúng cột và KHÔNG do client cộng ra', () => {
    const totals: readonly DataTableTotalRow[] = [
      { key: 'open', label: 'Số dư đầu tháng', position: 'above', cells: { amount: '9.999' } },
      { key: 'sum', label: 'Cộng phát sinh', cells: { amount: '1.750.000' } },
    ]
    renderTable({ totals })

    const rows = screen.getAllByRole('row')
    // Thứ tự: đầu bảng → dòng tổng 'above' → dữ liệu → dòng tổng 'below'
    expect(rows[1]).toHaveTextContent('Số dư đầu tháng')
    expect(rows[1]).toHaveTextContent('9.999')
    expect(rows[rows.length - 1]).toHaveTextContent('Cộng phát sinh')

    // Con số là thứ server gửi xuống, không phải tổng của hai dòng đang hiển
    // thị (1.500.000 + 250.000 = 1.750.000 chỉ là trùng hợp của dữ liệu mẫu —
    // điều được khẳng định là component hiện ĐÚNG chuỗi được truyền vào).
    expect(screen.getByText('1.750.000')).toBeInTheDocument()
  })

  it('dòng tổng không xuất hiện khi bảng rỗng', () => {
    renderTable({
      rows: [],
      totals: [{ key: 'sum', label: 'Cộng phát sinh', cells: {} }],
    })

    // "Cộng phát sinh: 0" trên một bảng không có dòng nào chỉ gây hiểu nhầm.
    expect(screen.queryByText('Cộng phát sinh')).not.toBeInTheDocument()
    expect(screen.getByText('Chưa có chứng từ nào.')).toBeInTheDocument()
  })

  it('nhãn dòng tổng là tiêu đề hàng cho trình đọc màn hình', () => {
    renderTable({ totals: [{ key: 'sum', label: 'Cộng phát sinh', cells: {} }] })

    expect(screen.getByRole('rowheader', { name: 'Cộng phát sinh' })).toBeInTheDocument()
  })

  it('zebra chỉ tô dòng chẵn, và chỉ khi được bật', () => {
    const { unmount } = renderTable({ zebra: true })
    let bodyRows = screen.getAllByRole('row').slice(1)
    expect(bodyRows[0]?.className).not.toContain('bg-row-alt')
    expect(bodyRows[1]?.className).toContain('bg-row-alt')
    unmount()

    renderTable()
    bodyRows = screen.getAllByRole('row').slice(1)
    expect(bodyRows[1]?.className).not.toContain('bg-row-alt')
  })

  it('số tiền không bị ngắt làm hai dòng', () => {
    renderTable()

    // "12.500.000,00" gãy giữa chừng là một con số đọc sai.
    expect(screen.getByText('1.500.000')).toHaveClass('whitespace-nowrap')
  })

  it('bảng tự giới thiệu bằng caption cho trình đọc màn hình', () => {
    renderTable()

    expect(screen.getByRole('table', { name: 'Phiếu chi trong kỳ' })).toBeInTheDocument()
  })
})
