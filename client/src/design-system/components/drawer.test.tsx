/**
 * Panel trượt — bốn quyết định trong docstring của `drawer.tsx`, bốn phép
 * khẳng định ở đây. Tất cả đều thuộc loại "hỏng thì mất dữ liệu người dùng vừa
 * gõ" hoặc "hỏng thì người dùng bàn phím bị nhốt", nên không cái nào được để
 * lại dạng ghi chú.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Button } from './button'
import { Drawer } from './drawer'

function renderDrawer(onClose = vi.fn(), open = true) {
  render(
    <Drawer open={open} title="HD-2026-000119" closeLabel="Đóng" onClose={onClose}>
      <input aria-label="Đối tác" />
      <input aria-label="Diễn giải" />
    </Drawer>,
  )
  return onClose
}

describe('Drawer', () => {
  it('không vẽ gì khi đóng', () => {
    renderDrawer(vi.fn(), false)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('là dialog có nhãn lấy từ tiêu đề', () => {
    renderDrawer()

    const dialog = screen.getByRole('dialog', { name: 'HD-2026-000119' })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('Esc đóng panel', async () => {
    const user = userEvent.setup()
    const onClose = renderDrawer()

    await user.keyboard('{Escape}')

    expect(onClose).toHaveBeenCalledOnce()
  })

  it('bấm ra nền KHÔNG đóng panel', async () => {
    const user = userEvent.setup()
    const onClose = renderDrawer()

    // Nền mờ là phần tử `aria-hidden` phía sau panel. Một cú bấm trượt tay
    // không được xóa sạch chứng từ đang gõ dở — đây là chỗ sản phẩm này cố ý
    // khác với mọi thư viện dialog thông dụng.
    const backdrop = document.querySelector('[aria-hidden]')
    expect(backdrop).not.toBeNull()
    await user.click(backdrop as Element)

    expect(onClose).not.toHaveBeenCalled()
  })

  it('nút đóng đóng panel', async () => {
    const user = userEvent.setup()
    const onClose = renderDrawer()

    await user.click(screen.getByRole('button', { name: 'Đóng' }))

    expect(onClose).toHaveBeenCalledOnce()
  })

  it('mở panel là focus vào Ô ĐẦU TIÊN của thân, không phải nút đóng', () => {
    renderDrawer()

    // Nút đóng đứng trước trong DOM nên nó mới là "phần tử focus được đầu
    // tiên"; người dùng mở panel để gõ, nên focus phải nhảy qua nó.
    expect(screen.getByLabelText('Đối tác')).toHaveFocus()
  })

  it('bẫy focus: Tab từ phần tử cuối vòng về phần tử đầu', async () => {
    const user = userEvent.setup()
    renderDrawer()

    await user.tab() // Đối tác → Diễn giải (phần tử cuối trong thứ tự DOM)
    expect(screen.getByLabelText('Diễn giải')).toHaveFocus()

    await user.tab() // → vòng về nút Đóng, KHÔNG rơi xuống sidebar phía sau
    expect(screen.getByRole('button', { name: 'Đóng' })).toHaveFocus()
  })

  it('bẫy focus: Shift+Tab từ phần tử đầu vòng về phần tử cuối', async () => {
    const user = userEvent.setup()
    renderDrawer()

    await user.tab({ shift: true }) // Đối tác → nút Đóng (đứng trước trong DOM)
    expect(screen.getByRole('button', { name: 'Đóng' })).toHaveFocus()

    await user.tab({ shift: true }) // → vòng về phần tử cuối
    expect(screen.getByLabelText('Diễn giải')).toHaveFocus()
  })

  it('bẫy focus cả khi thân panel không có ô nào focus được', async () => {
    const user = userEvent.setup()
    render(
      <Drawer open title="Chi tiết" closeLabel="Đóng" onClose={vi.fn()}>
        <p>Chỉ có chữ, không có ô nhập.</p>
      </Drawer>,
    )

    // Focus rơi về chính panel; Shift+Tab từ đó là đường thoát dễ quên nhất.
    expect(screen.getByRole('dialog')).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByRole('button', { name: 'Đóng' })).toHaveFocus()
  })

  it('bẫy focus không thủng vì phần tử ẨN khớp selector', async () => {
    const user = userEvent.setup()
    render(
      <Drawer open title="Chứng từ" closeLabel="Đóng" onClose={vi.fn()}>
        <input aria-label="Đối tác" />
        <input aria-label="Ô ẩn" style={{ display: 'none' }} />
      </Drawer>,
    )

    // `querySelectorAll` vẫn trả về ô ẩn. Nếu nó được tính là phần tử CUỐI thì
    // nhánh vòng lại không bao giờ chạy và phím Tab đi thẳng ra ngoài panel —
    // người dùng bàn phím gõ vào những ô phía sau mình không nhìn thấy. Ca thật:
    // form ẩn/hiện ô theo loại chứng từ, khối "Mở rộng" thu gọn.
    expect(screen.getByLabelText('Đối tác')).toHaveFocus()
    await user.tab()

    const dialog = screen.getByRole('dialog')
    expect(dialog.contains(document.activeElement)).toBe(true)
    expect(screen.getByRole('button', { name: 'Đóng' })).toHaveFocus()
  })

  it('trả focus về chỗ đã mở panel khi đóng', async () => {
    const user = userEvent.setup()

    function Host(): ReactElement {
      const [open, setOpen] = useState(false)
      return (
        <>
          <Button
            onClick={() => {
              setOpen(true)
            }}
          >
            Mở panel
          </Button>
          <Drawer
            open={open}
            title="Chi tiết"
            closeLabel="Đóng"
            onClose={() => {
              setOpen(false)
            }}
          >
            <input aria-label="Ghi chú" />
          </Drawer>
        </>
      )
    }

    render(<Host />)
    const opener = screen.getByRole('button', { name: 'Mở panel' })
    await user.click(opener)
    expect(screen.getByLabelText('Ghi chú')).toHaveFocus()

    await user.keyboard('{Escape}')

    // Người vừa mở panel từ dòng thứ 40 của bảng phải quay lại đúng dòng đó,
    // không phải quay về đầu trang.
    expect(opener).toHaveFocus()
  })

  it('không nổ khi phần tử đã mở panel bị gỡ khỏi DOM', async () => {
    const user = userEvent.setup()

    function Host(): ReactElement {
      const [open, setOpen] = useState(false)
      // Nút mở BIẾN MẤT khi panel mở — mô phỏng dòng bảng bị lọc đi sau khi
      // lưu, hoặc danh sách vừa tải lại.
      return (
        <>
          {!open && (
            <Button
              onClick={() => {
                setOpen(true)
              }}
            >
              Mở panel
            </Button>
          )}
          <Drawer
            open={open}
            title="Chi tiết"
            closeLabel="Đóng"
            onClose={() => {
              setOpen(false)
            }}
          >
            <input aria-label="Ghi chú" />
          </Drawer>
        </>
      )
    }

    render(<Host />)
    await user.click(screen.getByRole('button', { name: 'Mở panel' }))
    await user.keyboard('{Escape}')

    // `focus()` trên phần tử mồ côi im lặng không làm gì; điều phải giữ là
    // ứng dụng không nổ và panel đã đóng hẳn.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
