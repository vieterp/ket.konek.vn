/**
 * Thanh chia — kiểm phần **bàn phím** và phần **nhớ tỉ lệ**.
 *
 * Kéo bằng chuột không kiểm ở đây được: jsdom không có bố cục thật nên
 * `getBoundingClientRect()` trả bề rộng 0, và một bài test dựng số đo giả chỉ
 * kiểm chính cái số giả đó. Đường bàn phím thì kiểm được thật, và nó cũng là
 * đường dễ bị bỏ quên hơn khi viết component.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { SplitPane } from './split-pane'

function renderSplit(props: Partial<Parameters<typeof SplitPane>[0]> = {}) {
  render(
    <SplitPane
      separatorLabel="Kéo để đổi bề rộng hai khung"
      left={<p>Sao kê</p>}
      right={<p>Sổ kế toán</p>}
      {...props}
    />,
  )
  return screen.getByRole('separator')
}

describe('SplitPane', () => {
  it('thanh chia khai đủ giá trị cho trình đọc màn hình', () => {
    const separator = renderSplit()

    expect(separator).toHaveAttribute('aria-orientation', 'vertical')
    expect(separator).toHaveAttribute('aria-valuenow', '50')
    expect(separator).toHaveAttribute('aria-valuemin', '20')
    expect(separator).toHaveAttribute('aria-valuemax', '80')
  })

  it('mũi tên trái/phải xê dịch từng nấc', async () => {
    const user = userEvent.setup()
    const separator = renderSplit()

    await user.tab()
    expect(separator).toHaveFocus()

    await user.keyboard('{ArrowRight}')
    expect(separator).toHaveAttribute('aria-valuenow', '52')

    await user.keyboard('{ArrowLeft}{ArrowLeft}')
    expect(separator).toHaveAttribute('aria-valuenow', '48')
  })

  it('Home/End nhảy về hai biên và không vượt qua', async () => {
    const user = userEvent.setup()
    const separator = renderSplit()

    await user.tab()
    await user.keyboard('{Home}')
    expect(separator).toHaveAttribute('aria-valuenow', '20')

    // Đã ở biên trái rồi thì bấm tiếp không được lọt ra ngoài — khung trái biến
    // mất là trạng thái không quay lại được bằng chuột.
    await user.keyboard('{ArrowLeft}')
    expect(separator).toHaveAttribute('aria-valuenow', '20')

    await user.keyboard('{End}')
    expect(separator).toHaveAttribute('aria-valuenow', '80')
    await user.keyboard('{ArrowRight}')
    expect(separator).toHaveAttribute('aria-valuenow', '80')
  })

  it('nhớ tỉ lệ khi có storageKey', async () => {
    const user = userEvent.setup()
    renderSplit({ storageKey: 'reconcile' })

    await user.tab()
    await user.keyboard('{ArrowRight}')

    expect(localStorage.getItem('ket.split.reconcile')).toBe('52')
  })

  it('đọc lại tỉ lệ đã nhớ ở lần mở sau', () => {
    localStorage.setItem('ket.split.reconcile', '35')

    const separator = renderSplit({ storageKey: 'reconcile' })

    expect(separator).toHaveAttribute('aria-valuenow', '35')
  })

  it('bỏ qua giá trị đã nhớ nếu nó vô nghĩa', () => {
    // `Number('')` và `Number(null)` đều bằng 0 — không phân biệt được với một
    // tỉ lệ hợp lệ nếu chỉ kiểm `Number.isFinite`.
    localStorage.setItem('ket.split.reconcile', 'không phải số')

    const separator = renderSplit({ storageKey: 'reconcile', initialPercent: 60 })

    expect(separator).toHaveAttribute('aria-valuenow', '60')
  })

  it('không đụng localStorage khi không khai storageKey', async () => {
    const user = userEvent.setup()
    renderSplit()

    await user.tab()
    await user.keyboard('{ArrowRight}')

    expect(localStorage.length).toBe(0)
  })
})
