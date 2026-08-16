/**
 * Danh mục kiểm tra — thứ đáng kiểm nhất là **đường dẫn tới chỗ sửa**, vì đó là
 * điều phân biệt nó với một danh sách lỗi thông thường (nguyên tắc U11).
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { ChecklistItem } from './checklist-panel'
import { ChecklistPanel } from './checklist-panel'

describe('ChecklistPanel', () => {
  it('mục hỏng có nút dẫn tới chỗ sửa, mục đạt thì không', async () => {
    const user = userEvent.setup()
    const onFix = vi.fn()
    const items: readonly ChecklistItem[] = [
      { id: 'a', label: 'Mọi chứng từ đã ghi sổ', status: 'passed', statusLabel: 'Đạt' },
      {
        id: 'b',
        label: 'Không còn hóa đơn chờ phát hành',
        status: 'failed',
        statusLabel: 'Còn 3',
        detail: 'HD-000119, HD-000120, HD-000121.',
        fixLabel: 'Xem danh sách',
        onFix,
      },
    ]

    render(<ChecklistPanel title="Điều kiện khóa sổ" items={items} />)

    expect(screen.getByText('HD-000119, HD-000120, HD-000121.')).toBeInTheDocument()
    // Đúng một nút: mục đã đạt không có gì để sửa.
    const fixButtons = screen.getAllByRole('button')
    expect(fixButtons).toHaveLength(1)

    await user.click(screen.getByRole('button', { name: 'Xem danh sách' }))
    expect(onFix).toHaveBeenCalledOnce()
  })

  it('không vẽ nút sửa khi có nhãn nhưng thiếu hành vi', () => {
    const items: readonly ChecklistItem[] = [
      { id: 'a', label: 'Đã tính giá xuất', status: 'pending', statusLabel: 'Đang chạy', fixLabel: 'Sửa' },
    ]

    render(<ChecklistPanel title="Điều kiện khóa sổ" items={items} />)

    // Một nút bấm không làm gì làm người dùng tưởng đã làm rồi.
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('trạng thái hiện bằng CHỮ chứ không chỉ bằng màu và icon', () => {
    const items: readonly ChecklistItem[] = [
      { id: 'a', label: 'Cân đối Nợ/Có', status: 'failed', statusLabel: 'Lệch 1.200đ' },
    ]

    render(<ChecklistPanel title="Điều kiện khóa sổ" items={items} />)

    // Người không phân biệt được đỏ/xanh vẫn phải đọc được kết quả.
    expect(screen.getByText('Lệch 1.200đ')).toBeInTheDocument()
  })

  it('ánh xạ trạng thái → tông: mục HỎNG không bao giờ được hiện như mục ĐẠT', () => {
    const items: readonly ChecklistItem[] = [
      { id: 'p', label: 'Đã ghi sổ', status: 'passed', statusLabel: 'Đạt' },
      { id: 'f', label: 'Còn hóa đơn chờ', status: 'failed', statusLabel: 'Còn 3' },
      { id: 'w', label: 'Đang tính giá xuất', status: 'pending', statusLabel: 'Đang chạy' },
    ]

    render(<ChecklistPanel title="Điều kiện khóa sổ" items={items} />)

    // Kiểm đột biến ở lát trước cho thấy đổi `failed → 'success'` mà mọi test
    // vẫn xanh. Một mục khóa sổ không đạt hiện như đã đạt là hiểu nhầm nghiệp
    // vụ trực tiếp — kế toán viên khóa sổ khi chưa đủ điều kiện.
    expect(screen.getByText('Đạt')).toHaveAttribute('data-tone', 'ok')
    expect(screen.getByText('Còn 3')).toHaveAttribute('data-tone', 'bad')
    // "Đang chạy" là việc CÒN PHẢI LÀM, nên nó là `todo` chứ không phải `ok`.
    expect(screen.getByText('Đang chạy')).toHaveAttribute('data-tone', 'todo')
  })

  it('câu tổng kết do chỗ gọi truyền vào, component không tự suy', () => {
    render(
      <ChecklistPanel
        title="Điều kiện khóa sổ"
        summary="Chưa đủ điều kiện — còn 1 mục chưa đạt."
        items={[{ id: 'a', label: 'X', status: 'passed', statusLabel: 'Đạt' }]}
      />,
    )

    // Mọi mục đều "đạt" mà tổng kết vẫn nói "chưa đủ": luật nghiệp vụ nằm ở
    // server (LD-03), component không được tự kết luận thay.
    expect(screen.getByText('Chưa đủ điều kiện — còn 1 mục chưa đạt.')).toBeInTheDocument()
  })
})
