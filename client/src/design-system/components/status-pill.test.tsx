/**
 * Nhãn trạng thái + ô "Việc tiếp theo" — hai component nhỏ, gộp một tệp test.
 *
 * Điều đáng khóa lại không phải mã mà là **giao ước**: ba tông phải trông KHÁC
 * nhau, trạng thái phải đọc được bằng chữ, và ô việc không bao giờ để trống.
 *
 * Bài "ba tông khác nhau" có mặt vì một lý do cụ thể: kiểm đột biến ở lát trước
 * cho thấy đổi tông `danger` thành y hệt `neutral` mà **toàn bộ test vẫn xanh**.
 * Một bảng trong đó "Bị từ chối" trông giống hệt "Nháp" là hiểu nhầm nghiệp vụ
 * trực tiếp, và không có phép khẳng định nào bắt được.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { NextActionCell } from './next-action-cell'
import type { StatusTone } from './status-pill'
import { StatusPill } from './status-pill'

const TONES: readonly StatusTone[] = ['ok', 'todo', 'bad']

describe('StatusPill', () => {
  it('luôn có chữ, không chỉ có màu', () => {
    render(<StatusPill tone="bad">Bị từ chối</StatusPill>)

    expect(screen.getByText('Bị từ chối')).toBeInTheDocument()
  })

  it('BA tông vẽ ra ba bộ lớp KHÁC nhau', () => {
    const classNames = TONES.map((tone) => {
      const { unmount } = render(<StatusPill tone={tone}>Nhãn</StatusPill>)
      const className = screen.getByText('Nhãn').className
      unmount()
      return className
    })

    // Không chỉ kiểm "khác cái trước": nếu hai tông bất kỳ trùng nhau thì tập
    // hợp sẽ nhỏ hơn ba.
    expect(new Set(classNames).size).toBe(TONES.length)
  })

  it('mỗi tông ghi lại chính nó trong data-tone', () => {
    for (const tone of TONES) {
      const { unmount } = render(<StatusPill tone={tone}>Nhãn</StatusPill>)
      expect(screen.getByText('Nhãn')).toHaveAttribute('data-tone', tone)
      unmount()
    }
  })

  it('mặc định là tông "xong" (ok)', () => {
    render(<StatusPill>Đã ghi sổ</StatusPill>)

    expect(screen.getByText('Đã ghi sổ')).toHaveAttribute('data-tone', 'ok')
  })

  it('chấm chỉ để trang trí — trình đọc màn hình bỏ qua', () => {
    render(<StatusPill tone="todo">Chờ phát hành</StatusPill>)

    // Chấm mang thông tin trùng với chữ; đọc thêm nó chỉ làm rối.
    const dot = screen.getByText('Chờ phát hành').querySelector('[aria-hidden]')
    expect(dot).not.toBeNull()
  })
})

describe('NextActionCell', () => {
  it('không còn việc thì hiện nhãn "đã xong", không để ô trống', () => {
    render(<NextActionCell action={null} doneLabel="Đã xong" />)

    // Ô trống là thứ người dùng phải tự đoán: chưa tải xong, hay xong rồi?
    expect(screen.getByText('Đã xong')).toBeInTheDocument()
  })

  it('còn việc thì hiện câu việc kèm nút làm ngay', async () => {
    const user = userEvent.setup()
    const onAction = vi.fn()
    render(
      <NextActionCell
        action="Lập hóa đơn thay thế"
        doneLabel="Đã xong"
        actionLabel="Làm ngay"
        onAction={onAction}
      />,
    )

    expect(screen.getByText('Lập hóa đơn thay thế')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Làm ngay' }))
    expect(onAction).toHaveBeenCalledOnce()
  })

  it('có câu việc nhưng không có hành vi thì không vẽ nút', () => {
    render(<NextActionCell action="Chờ cơ quan thuế cấp mã" doneLabel="Đã xong" actionLabel="Làm ngay" />)

    expect(screen.getByText('Chờ cơ quan thuế cấp mã')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
