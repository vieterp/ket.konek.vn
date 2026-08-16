/**
 * Tab — kiểm đúng những gì `tsc` không kiểm được: bàn phím và quan hệ ARIA.
 *
 * Trọng tâm là **kích hoạt bằng tay**: mũi tên chỉ di chuyển focus, không đổi
 * tab. Đây là quyết định đắt (xem docstring của `tabs.tsx`) và cũng là thứ dễ
 * bị "sửa lại cho tiện" nhất trong một lần refactor sau này — nên nó phải là
 * một phép khẳng định, không phải một câu ghi chú.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Tabs } from './tabs'

const TABS = [
  { id: 'all', label: 'Tất cả', count: 12 },
  { id: 'todo', label: 'Còn việc', count: 3 },
  { id: 'done', label: 'Đã xong' },
]

function renderTabs(onChange = vi.fn(), activeId = 'all') {
  render(
    <Tabs tabs={TABS} activeId={activeId} onChange={onChange} label="Lọc theo trạng thái">
      <p>Nội dung tab {activeId}</p>
    </Tabs>,
  )
  return onChange
}

describe('Tabs', () => {
  it('nối panel với tab đang chọn bằng aria-controls/aria-labelledby', () => {
    renderTabs()

    const active = screen.getByRole('tab', { name: /Tất cả/ })
    const panel = screen.getByRole('tabpanel')

    expect(active).toHaveAttribute('aria-selected', 'true')
    expect(active.getAttribute('aria-controls')).toBe(panel.getAttribute('id'))
    expect(panel.getAttribute('aria-labelledby')).toBe(active.getAttribute('id'))
  })

  it('cả dải tab chỉ là MỘT điểm dừng của phím Tab (roving tabindex)', () => {
    renderTabs()

    expect(screen.getByRole('tab', { name: /Tất cả/ })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: /Còn việc/ })).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('tab', { name: 'Đã xong' })).toHaveAttribute('tabindex', '-1')
  })

  it('mũi tên chỉ chuyển focus, KHÔNG đổi tab', async () => {
    const user = userEvent.setup()
    const onChange = renderTabs()

    await user.tab()
    expect(screen.getByRole('tab', { name: /Tất cả/ })).toHaveFocus()

    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: /Còn việc/ })).toHaveFocus()
    // Đây là phép khẳng định quan trọng nhất của tệp này: mỗi lần đổi tab là
    // một truy vấn danh sách xuống server, nên lướt qua tab không được bắn nó.
    expect(onChange).not.toHaveBeenCalled()

    await user.keyboard('{Enter}')
    expect(onChange).toHaveBeenCalledExactlyOnceWith('todo')
  })

  it('mũi tên vòng lại ở hai đầu, Home/End nhảy về hai biên', async () => {
    const user = userEvent.setup()
    renderTabs()

    await user.tab()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('tab', { name: 'Đã xong' })).toHaveFocus()

    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: /Tất cả/ })).toHaveFocus()

    await user.keyboard('{End}')
    expect(screen.getByRole('tab', { name: 'Đã xong' })).toHaveFocus()

    await user.keyboard('{Home}')
    expect(screen.getByRole('tab', { name: /Tất cả/ })).toHaveFocus()
  })

  it('activeId không khớp tab nào thì rơi về tab đầu, KHÔNG mất khỏi bàn phím', () => {
    // Xảy ra thật: tab được lọc theo quyền/kỳ rồi tab đang chọn biến mất, hoặc
    // activeId khôi phục từ một dấu trang cũ. Không xử lý thì mọi tab đều
    // tabIndex=-1 — người dùng bàn phím mất hẳn đường tới bộ lọc.
    render(
      <Tabs tabs={TABS} activeId="tab-đã-biến-mất" onChange={vi.fn()} label="Lọc">
        <p>Nội dung</p>
      </Tabs>,
    )

    const first = screen.getByRole('tab', { name: /Tất cả/ })
    expect(first).toHaveAttribute('tabindex', '0')
    expect(first).toHaveAttribute('aria-selected', 'true')

    // Và panel phải trỏ vào một id CÓ THẬT, nếu không trình đọc màn hình đọc
    // một panel không tên.
    const panel = screen.getByRole('tabpanel')
    expect(panel.getAttribute('aria-labelledby')).toBe(first.getAttribute('id'))
  })

  it('hiện số việc của tab, kể cả khi số đó bằng 0', () => {
    render(
      <Tabs
        tabs={[{ id: 'x', label: 'Kỳ trước', count: 0 }]}
        activeId="x"
        onChange={vi.fn()}
        label="Lọc"
      >
        <p />
      </Tabs>,
    )

    // `0` là thông tin thật ("không còn việc"), không phải giá trị vắng mặt —
    // một lần viết `{count && …}` là số 0 biến mất khỏi màn hình.
    expect(screen.getByRole('tab', { name: /Kỳ trước\s*0/ })).toBeInTheDocument()
  })
})
