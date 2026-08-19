/**
 * Khối "Mở rộng" — đóng theo mặc định, mở/đóng bằng nút, `aria-expanded` khớp
 * trạng thái đang vẽ (bàn phím/trình đọc màn hình dựa vào đúng thuộc tính này).
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { AdvancedSection } from './advanced-section'

describe('AdvancedSection', () => {
  it('đóng theo mặc định — nội dung không vẽ ra DOM', () => {
    render(
      <AdvancedSection label="Mở rộng">
        <input aria-label="Tỷ giá" />
      </AdvancedSection>,
    )

    expect(screen.queryByLabelText('Tỷ giá')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mở rộng' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('bấm nút thì mở ra, bấm lại thì đóng', async () => {
    const user = userEvent.setup()
    render(
      <AdvancedSection label="Mở rộng">
        <input aria-label="Tỷ giá" />
      </AdvancedSection>,
    )

    await user.click(screen.getByRole('button', { name: 'Mở rộng' }))
    expect(screen.getByLabelText('Tỷ giá')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Mở rộng' })).toHaveAttribute(
      'aria-expanded',
      'true',
    )

    await user.click(screen.getByRole('button', { name: 'Mở rộng' }))
    expect(screen.queryByLabelText('Tỷ giá')).not.toBeInTheDocument()
  })

  it('`defaultOpen` mở sẵn từ lần vẽ đầu — form sửa có giá trị khác mặc định', () => {
    render(
      <AdvancedSection label="Mở rộng" defaultOpen>
        <input aria-label="Tỷ giá" />
      </AdvancedSection>,
    )

    expect(screen.getByLabelText('Tỷ giá')).toBeInTheDocument()
  })
})
