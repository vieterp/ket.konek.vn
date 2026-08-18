/**
 * Combobox tham chiếu — kiểm lọc theo mã lẫn tên, chọn bằng chuột và bàn phím,
 * và nút bỏ chọn. Toàn bộ dữ liệu là mảng tĩnh: lọc xảy ra trong bộ nhớ.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { LookupOption } from './lookup-input'
import { LookupInput } from './lookup-input'

const OPTIONS: readonly LookupOption[] = [
  { id: 1, code: 'VCB', label: 'Ngoại thương Việt Nam' },
  { id: 2, code: 'ACB', label: 'Á Châu' },
  { id: 3, code: 'BIDV', label: 'Đầu tư và Phát triển' },
]

function renderInput(
  onChange: (option: LookupOption | null) => void,
  value: LookupOption | null = null,
): void {
  render(
    <LookupInput
      label="Ngân hàng"
      value={value}
      onChange={onChange}
      options={OPTIONS}
      clearLabel="Bỏ chọn"
      emptyLabel="Không có bản ghi nào"
    />,
  )
}

describe('LookupInput', () => {
  it('gõ mã thì lọc còn đúng gợi ý, bấm chọn trả về bản ghi', async () => {
    const onChange = vi.fn()
    renderInput(onChange)
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Ngân hàng'), 'vcb')
    expect(screen.getByText('VCB — Ngoại thương Việt Nam')).toBeInTheDocument()
    expect(screen.queryByText('ACB — Á Châu')).not.toBeInTheDocument()

    await user.click(screen.getByText('VCB — Ngoại thương Việt Nam'))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
  })

  it('lọc được theo tên, không chỉ theo mã', async () => {
    renderInput(vi.fn())
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Ngân hàng'), 'á châu')
    expect(screen.getByText('ACB — Á Châu')).toBeInTheDocument()
    expect(screen.queryByText('BIDV — Đầu tư và Phát triển')).not.toBeInTheDocument()
  })

  it('↓ rồi Enter chọn gợi ý đang trỏ', async () => {
    const onChange = vi.fn()
    renderInput(onChange)
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Ngân hàng'), 'b')
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('không khớp gì thì nói "không có bản ghi nào"', async () => {
    renderInput(vi.fn())
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Ngân hàng'), 'xyz-khong-co')
    expect(screen.getByText('Không có bản ghi nào')).toBeInTheDocument()
  })

  it('đã chọn thì hiện giá trị + nút bỏ chọn', async () => {
    const onChange = vi.fn()
    renderInput(onChange, OPTIONS[1] ?? null)
    const user = userEvent.setup()

    expect(screen.getByText('ACB — Á Châu')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Bỏ chọn' }))
    expect(onChange).toHaveBeenCalledWith(null)
  })
})
