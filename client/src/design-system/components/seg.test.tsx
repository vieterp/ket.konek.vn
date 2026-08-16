/**
 * Điều khiển phân đoạn — khác `Tabs` ở chỗ mũi tên **chọn luôn**, không chỉ
 * chuyển focus. Đó là khác biệt có chủ đích (xem docstring `seg.tsx`) nên nó
 * phải là một phép khẳng định, nếu không lần refactor sau sẽ "thống nhất" hai
 * component lại với nhau và làm hỏng một trong hai.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Seg } from './seg'

const OPTIONS = [
  { value: 'no', label: 'Chưa kê khai' },
  { value: 'yes', label: 'Đã kê khai' },
  { value: 'partial', label: 'Kê khai một phần' },
]

function renderSeg(onChange = vi.fn(), value = 'no') {
  render(<Seg options={OPTIONS} value={value} onChange={onChange} label="Tình trạng kê khai" />)
  return onChange
}

describe('Seg', () => {
  it('là nhóm radio, không phải dải tab', () => {
    renderSeg()

    // `radiogroup` để trình đọc màn hình đọc "2 trong 3" — đúng thứ người dùng
    // cần biết khi đang CHỌN MỘT GIÁ TRỊ, khác hẳn khi đang đi tới một chỗ khác.
    expect(screen.getByRole('radiogroup', { name: 'Tình trạng kê khai' })).toBeInTheDocument()
    expect(screen.getAllByRole('radio')).toHaveLength(3)
    expect(screen.getByRole('radio', { name: 'Chưa kê khai' })).toBeChecked()
  })

  it('cả nhóm chỉ là MỘT điểm dừng của phím Tab', () => {
    renderSeg(vi.fn(), 'yes')

    expect(screen.getByRole('radio', { name: 'Chưa kê khai' })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('radio', { name: 'Đã kê khai' })).toHaveAttribute('tabindex', '0')
  })

  it('mũi tên CHỌN LUÔN — khác Tabs (chỉ chuyển focus)', async () => {
    const user = userEvent.setup()
    const onChange = renderSeg()

    await user.tab()
    await user.keyboard('{ArrowRight}')

    // Đổi một giá trị trên form không tốn vòng gọi server như đổi tab danh sách,
    // nên ở đây kiểu radio gốc của nền tảng là đúng.
    expect(onChange).toHaveBeenCalledExactlyOnceWith('yes')
  })

  it('mũi tên vòng lại hai đầu, Home/End về hai biên', async () => {
    const user = userEvent.setup()
    const onChange = renderSeg()

    await user.tab()
    await user.keyboard('{ArrowLeft}')
    expect(onChange).toHaveBeenLastCalledWith('partial')

    await user.keyboard('{End}')
    expect(onChange).toHaveBeenLastCalledWith('partial')

    await user.keyboard('{Home}')
    expect(onChange).toHaveBeenLastCalledWith('no')
  })

  it('bấm chuột chọn đúng phương án', async () => {
    const user = userEvent.setup()
    const onChange = renderSeg()

    await user.click(screen.getByRole('radio', { name: 'Kê khai một phần' }))

    expect(onChange).toHaveBeenCalledExactlyOnceWith('partial')
  })
})
