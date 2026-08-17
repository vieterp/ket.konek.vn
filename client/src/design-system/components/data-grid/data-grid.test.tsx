/**
 * Lưới nhập liệu — khóa lại ba quyết định làm nên hiệu năng của nó.
 *
 * Ngưỡng thời gian (< 50ms mỗi phím, < 1s cho 200 dòng dán) **không** đo ở đây:
 * jsdom không có bố cục và không vẽ gì, nên mọi con số đo trong nó là số giả.
 * Đo thật nằm ở `client/bench/data-grid.bench.spec.ts` chạy trên Chromium.
 *
 * Chỗ này khóa thứ khác, và là thứ dễ mất hơn trong lần refactor sau: **cơ chế**
 * sinh ra hiệu năng đó (đúng một ô nhập, cửa sổ dòng) cùng những hành vi mà một
 * bài đo không bắt được (IME, dán vùng, Escape).
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { DataGrid } from './data-grid'
import type {
  DataGridCell,
  DataGridChange,
  DataGridColumn,
  DataGridCommitMode,
} from './types'

interface Line {
  readonly id: string
  readonly item: string
  readonly qty: string
  readonly price: string
  readonly amount: string
}

const COLUMNS: readonly DataGridColumn<Line>[] = [
  { key: 'item', header: 'Hàng hóa', value: (row) => row.item },
  { key: 'qty', header: 'Số lượng', align: 'right', inputMode: 'decimal', value: (row) => row.qty },
  { key: 'price', header: 'Đơn giá', align: 'right', inputMode: 'decimal', value: (row) => row.price },
  // Cột do server tính — lưới không nhân số lượng với đơn giá (H15).
  { key: 'amount', header: 'Thành tiền', align: 'right', readOnly: true, value: (row) => row.amount },
]

function emptyLine(index: number): Line {
  return { id: `row-${index}`, item: '', qty: '', price: '', amount: '' }
}

function makeLines(count: number): Line[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `row-${index}`,
    item: `Hàng ${index}`,
    qty: `${index}`,
    price: '100000',
    amount: '0',
  }))
}

interface HarnessProps {
  readonly rowCount?: number
  readonly commitMode?: DataGridCommitMode
  readonly onCommit?: (changes: readonly DataGridChange[]) => void
  readonly onActiveCellChange?: (cell: DataGridCell) => void
  readonly columns?: readonly DataGridColumn<Line>[]
  /** `false` = chỗ gọi nhận `onCommit` nhưng KHÔNG phản chiếu lại (lưu chậm, server chuẩn hóa). */
  readonly reflect?: boolean
}

/** Chỗ gọi thật: giữ trạng thái dòng và tự nới thêm dòng khi vùng dán tràn. */
function Harness({
  rowCount = 3,
  commitMode,
  onCommit,
  onActiveCellChange,
  columns = COLUMNS,
  reflect = true,
}: HarnessProps): ReactElement {
  const [lines, setLines] = useState<Line[]>(() => makeLines(rowCount))

  return (
    <DataGrid
      columns={columns}
      rows={lines}
      rowKey={(row) => row.id}
      caption="Chi tiết chứng từ"
      cellLabel={(header, rowNumber) => `${header}, dòng ${rowNumber}`}
      rowHeight={30}
      height={300}
      {...(commitMode === undefined ? {} : { commitMode })}
      {...(onActiveCellChange === undefined ? {} : { onActiveCellChange })}
      onCommit={(changes) => {
        onCommit?.(changes)
        if (!reflect) {
          return
        }
        setLines((previous) => {
          const next = [...previous]
          for (const change of changes) {
            while (next.length <= change.rowIndex) {
              next.push(emptyLine(next.length))
            }
            const current = next[change.rowIndex]
            if (current !== undefined) {
              next[change.rowIndex] = { ...current, [change.columnKey]: change.value }
            }
          }
          return next
        })
      }}
    />
  )
}

/** Cuộn vùng dòng — jsdom không tự bắn `scroll`, và cửa sổ dòng nghe sự kiện đó. */
async function scrollTo(container: HTMLElement, top: number): Promise<void> {
  const viewport = container.querySelector('.overflow-auto')
  if (viewport === null) {
    throw new Error('không tìm thấy vùng cuộn của lưới')
  }
  await act(async () => {
    viewport.scrollTop = top
    fireEvent.scroll(viewport)
    await new Promise((resolve) => {
      requestAnimationFrame(() => {
        resolve(null)
      })
    })
  })
}

function activeInput(): HTMLInputElement {
  const inputs = screen.getAllByRole('textbox')
  expect(inputs).toHaveLength(1)
  return inputs[0] as HTMLInputElement
}

describe('DataGrid — H11: đúng một ô nhập, và chỉ vẽ cửa sổ dòng', () => {
  it('500 dòng vẫn chỉ có MỘT `<input>` trong DOM', () => {
    render(<Harness rowCount={500} />)

    // 500 × 3 cột sửa được = 1.500 ô nhập nếu làm theo lối ngây thơ. Mỗi phím
    // gõ, trình duyệt và React đều phải đi qua từng ấy node.
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
  })

  it('500 dòng nhưng chỉ vẽ số dòng vừa vùng cuộn', () => {
    render(<Harness rowCount={500} />)

    // Vùng cuộn 300px / dòng 30px = 10 dòng nhìn thấy, cộng đệm hai đầu.
    const rendered = screen.getAllByRole('row').length - 1
    expect(rendered).toBeLessThan(30)
    expect(screen.getByRole('grid', { name: 'Chi tiết chứng từ' })).toHaveAttribute(
      'aria-rowcount',
      '501',
    )
  })

  it('cột chỉ-đọc không bao giờ thành ô nhập', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    await user.click(screen.getAllByRole('gridcell')[3] as HTMLElement)

    // Bấm vào "Thành tiền" không mở ô nhập nào; ô đang chọn vẫn là ô cũ.
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 1')
  })
})

describe('DataGrid — điều hướng bàn phím', () => {
  it('Tab sang cột sửa được kế tiếp rồi vắt sang dòng sau', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    activeInput().focus()
    await user.keyboard('{Tab}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 1')

    await user.keyboard('{Tab}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Đơn giá, dòng 1')

    // Cột "Thành tiền" bị bỏ qua — Tab vào một ô không sửa được là một lần
    // gõ hụt trên mỗi dòng, cả ngày.
    await user.keyboard('{Tab}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 2')

    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Đơn giá, dòng 1')
  })

  it('Enter và mũi tên dọc đi xuống cùng cột', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    activeInput().focus()
    await user.keyboard('{Tab}{Enter}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 2')

    await user.keyboard('{ArrowDown}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 3')

    // Đến đáy thì đứng yên, không vòng lên đầu: vòng lại làm người đang gõ
    // nhanh ghi đè lên dòng đầu tiên mà không nhận ra.
    await user.keyboard('{ArrowDown}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 3')
  })

  it('mũi tên ngang di con trỏ trong chữ, chỉ nhảy ô khi đã ở mép', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    // Phải đứng ở cột GIỮA. Ở cột đầu của dòng đầu thì `moveHorizontal(-1)` trả
    // `false` vì hết lưới, nên bài test xanh dù hàng rào con trỏ còn hay mất —
    // đó đúng là cách bản đầu của bài test này rỗng ruột.
    activeInput().focus()
    await user.keyboard('{Tab}')
    const input = activeInput()
    expect(input).toHaveAttribute('aria-label', 'Số lượng, dòng 1')

    input.setSelectionRange(1, 1)
    await user.keyboard('{ArrowLeft}')
    // Sửa một chữ số ở giữa "12500000" mà phải với chuột thì lưới hỏng.
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 1')

    input.setSelectionRange(0, 0)
    await user.keyboard('{ArrowLeft}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 1')
  })

  it('Tab ở ô cuối lưới THẢ cho trình duyệt — không nhốt bàn phím', async () => {
    const user = userEvent.setup()
    render(
      <>
        <Harness rowCount={1} />
        <button type="button">Lưu</button>
      </>,
    )

    activeInput().focus()
    await user.keyboard('{Tab}{Tab}{Tab}')

    // Ô sửa được cuối cùng của lưới. Nuốt luôn phím Tab ở đây là một lưới không
    // thoát ra được bằng bàn phím (WCAG 2.1.2) — người dùng không tới được nút
    // "Lưu" nếu không với chuột.
    expect(screen.getByRole('button', { name: 'Lưu' })).toHaveFocus()
  })

  it('Enter luôn chặn mặc định — lưới nằm trong form thì Enter là GỬI FORM', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn((event: React.FormEvent) => {
      event.preventDefault()
    })
    render(
      <form onSubmit={onSubmit}>
        <Harness rowCount={1} />
      </form>,
    )

    activeInput().focus()
    // Dòng duy nhất → Enter không đi đâu được, nhưng vẫn phải chặn: gửi form ở
    // đây là ghi sổ một chứng từ đang gõ dở.
    await user.keyboard('{Enter}')

    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('DataGrid — trạng thái chưa cam kết không được rơi mất', () => {
  it('cuộn dòng đang gõ ra khỏi cửa sổ vẫn CAM KẾT được chữ vừa gõ', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    const { container } = render(<Harness rowCount={500} onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('{Control>}a{/Control}')
    await user.keyboard('Bút bi')

    // Lăn chuột xuống xem tổng cộng ở cuối lưới là thao tác hằng ngày. Trình
    // duyệt KHÔNG bắn `blur` khi phần tử đang focus bị gỡ khỏi DOM, nên nếu chỉ
    // trông vào `onBlur` thì chữ vừa gõ biến mất không dấu vết.
    await scrollTo(container, 30 * 120)

    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(onCommit).toHaveBeenCalledExactlyOnceWith([
      { rowIndex: 0, columnKey: 'item', value: 'Bút bi' },
    ])
  })

  it('chỗ gọi đổi bản ghi dưới chân ô đang gõ → BỎ thay đổi, không ghi sang dòng khác', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()

    function ShiftingHarness(): ReactElement {
      const [lines, setLines] = useState<Line[]>(() => makeLines(3))
      return (
        <>
          <button
            type="button"
            // Không lấy focus — mô phỏng đường xóa dòng KHÔNG đi qua `blur`:
            // phím tắt, hoặc dữ liệu về từ server. Đường có `blur` thì ô đã
            // được chốt trước khi dữ liệu đổi nên không phải ca đáng lo.
            onMouseDown={(event) => {
              event.preventDefault()
            }}
            onClick={() => {
              setLines((previous) => previous.slice(1))
            }}
          >
            Xóa dòng đầu
          </button>
          <DataGrid
            columns={COLUMNS}
            rows={lines}
            rowKey={(row) => row.id}
            caption="Chi tiết chứng từ"
            cellLabel={(header, rowNumber) => `${header}, dòng ${rowNumber}`}
            rowHeight={30}
            height={300}
            onCommit={onCommit}
          />
        </>
      )
    }

    render(<ShiftingHarness />)
    activeInput().focus()
    await user.keyboard('XYZ')
    await user.click(screen.getByRole('button', { name: 'Xóa dòng đầu' }))
    // Rời ô sau khi dữ liệu đã đổi — đây mới là lúc lưới định ghi xuống.
    await user.keyboard('{Tab}')

    // Vị trí 0 giờ là một chứng từ KHÁC. Ghi chữ của người dùng vào đó là hỏng
    // sổ; bỏ một ô chưa lưu thì chỉ mất một ô.
    //
    // Cơ chế là khóa React của dòng: nó lấy từ `rowKey`, nên bản ghi ở vị trí
    // ấy đổi thì cả dòng lẫn ô nhập được dựng lại và chữ chưa cam kết bị bỏ.
    // Đây cũng là lý do `rowKey` phải là định danh THẬT của bản ghi — truyền
    // chỉ số dòng làm khóa thì mất hẳn hàng rào này.
    expect(onCommit).not.toHaveBeenCalled()
    expect(activeInput().value).toBe('Hàng 1')
  })

  it('chỗ gọi không phản chiếu lại thì cũng KHÔNG cam kết trùng', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    const { rerender } = render(<Harness onCommit={onCommit} reflect={false} />)

    activeInput().focus()
    await user.keyboard('X')
    fireEvent.blur(activeInput())
    expect(onCommit).toHaveBeenCalledTimes(1)

    // Lưu chậm / server chuẩn hóa / validate từ chối — `rows` chưa đổi. Một lượt
    // vẽ lại bất kỳ của chỗ gọi không được biến một thay đổi thành hai lần ghi.
    rerender(<Harness onCommit={onCommit} reflect={false} />)
    fireEvent.blur(activeInput())

    expect(onCommit).toHaveBeenCalledTimes(1)
  })
})

describe('DataGrid — H12: ô đang gõ không controlled', () => {
  it('gõ không cam kết gì; rời ô mới cam kết một lần', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('Bút bi')

    // Không lượt vẽ lại nào trong lúc gõ — đó chính là chỗ độ trễ phím biến mất.
    expect(onCommit).not.toHaveBeenCalled()

    await user.keyboard('{Tab}')
    expect(onCommit).toHaveBeenCalledExactlyOnceWith([
      { rowIndex: 0, columnKey: 'item', value: 'Hàng 0Bút bi' },
    ])
  })

  it('chế độ `live` cam kết từng phím — dùng cho màn hình cộng dồn tại chỗ', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    render(<Harness commitMode="live" onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('ab')

    expect(onCommit).toHaveBeenCalledTimes(2)
  })

  it('tổ hợp IME không bị cắt ngang, và Enter kết thúc tổ hợp không nhảy dòng', () => {
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    const input = activeInput()
    input.focus()
    fireEvent.compositionStart(input)
    fireEvent.change(input, { target: { value: 'dduowcj' } })

    // Phím Enter xác nhận một tổ hợp Telex và phím Enter xuống dòng dưới là
    // cùng một sự kiện `keydown`. Không phân biệt thì gõ xong "được" là mất
    // dòng — và người dùng chỉ thấy "thỉnh thoảng nhảy lung tung".
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true })
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 1')
    expect(activeInput().value).toBe('dduowcj')

    // Vế thứ hai của hàng rào, và nó phải có phép khẳng định RIÊNG: có bộ gõ
    // không đặt `isComposing` trên `keydown` (Safari/WebKit là ca đã biết), nên
    // cờ `compositionstart` của ta là đường phòng thủ duy nhất khi ấy.
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 1')

    fireEvent.compositionEnd(input)
    fireEvent.change(input, { target: { value: 'được' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 2')
    expect(onCommit).toHaveBeenCalledExactlyOnceWith([
      { rowIndex: 0, columnKey: 'item', value: 'được' },
    ])
  })

  it('`isComposing` trên phím là đủ để chặn, dù chưa thấy `compositionstart`', () => {
    render(<Harness />)

    const input = activeInput()
    input.focus()
    // Không có `compositionstart` nào — mô phỏng trình duyệt chỉ đặt cờ trên
    // chính sự kiện phím. Hai vế của hàng rào phải độc lập canh được nhau.
    fireEvent.keyDown(input, { key: 'ArrowDown', isComposing: true })

    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 1')
  })

  it('bỏ dở một tổ hợp Telex rồi bấm sang ô khác — bàn phím của lưới VẪN SỐNG', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    const input = activeInput()
    input.focus()
    fireEvent.compositionStart(input)
    fireEvent.change(input, { target: { value: 'dduo' } })

    // Bấm chuột sang ô khác giữa chừng: ô nhập cũ bị tháo nên `compositionend`
    // KHÔNG bao giờ bắn. Không đặt lại cờ thì nó kẹt `true` vĩnh viễn — Tab,
    // Enter, mũi tên, Escape chết hết trong khi chữ vẫn gõ được, và không ai
    // đoán ra nguyên nhân.
    await user.click(screen.getAllByRole('gridcell')[1] as HTMLElement)
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 1')

    await user.keyboard('{ArrowDown}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Số lượng, dòng 2')
  })

  it('dán vùng KHÔNG được cắt ngang một tổ hợp IME', () => {
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    const input = activeInput()
    input.focus()
    fireEvent.compositionStart(input)
    // Dán không đi qua `handleKeyDown`, nên hàng rào IME phải viết riêng: dựng
    // lại ô nhập giữa tổ hợp làm bộ gõ mất chỗ bám, chữ đang soạn rơi mất.
    fireEvent.paste(input, { clipboardData: { getData: () => 'a\tb\nc\td' } })

    expect(onCommit).not.toHaveBeenCalled()
  })

  it('Escape ở `live` hoàn tác về giá trị lúc VÀO ô, và cam kết hoàn tác', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    render(<Harness commitMode="live" onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('99')
    await user.keyboard('{Escape}')

    // Ở `live` những phím đã gõ ĐÃ đi lên chỗ gọi, nên Escape chỉ sửa cái nhìn
    // thấy là chưa đủ — dữ liệu thật vẫn mang con số gõ nhầm.
    expect(activeInput().value).toBe('Hàng 0')
    expect(onCommit).toHaveBeenLastCalledWith([
      { rowIndex: 0, columnKey: 'item', value: 'Hàng 0' },
    ])
  })

  it('Escape trả ô về giá trị nền và không cam kết gì', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('xxx{Escape}')

    expect(activeInput().value).toBe('Hàng 0')

    await user.keyboard('{Tab}')
    expect(onCommit).not.toHaveBeenCalled()
  })
})

describe('DataGrid — H14: dán vùng từ bảng tính', () => {
  function paste(text: string): void {
    fireEvent.paste(activeInput(), { clipboardData: { getData: () => text } })
  }

  it('dán vùng nhiều ô đi lên chỗ gọi trong MỘT lượt', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('{Tab}')
    paste('2\t50000\n3\t70000')

    // Một lượt gọi, không phải bốn: chỗ gọi cập nhật trạng thái đúng một lần
    // thay vì kéo theo bốn lượt vẽ lại.
    expect(onCommit).toHaveBeenCalledExactlyOnceWith([
      { rowIndex: 0, columnKey: 'qty', value: '2' },
      { rowIndex: 0, columnKey: 'price', value: '50000' },
      { rowIndex: 1, columnKey: 'qty', value: '3' },
      { rowIndex: 1, columnKey: 'price', value: '70000' },
    ])
  })

  it('ô đang chọn hiện giá trị vừa dán đè lên, không phải giá trị cũ', async () => {
    const user = userEvent.setup()
    render(<Harness />)

    activeInput().focus()
    await user.keyboard('{Tab}')
    paste('2\t50000\n3\t70000')

    expect(activeInput().value).toBe('2')
  })

  it('bỏ qua cột chỉ-đọc và cột tràn ra ngoài, không dồn sang cột kế bên', async () => {
    const user = userEvent.setup()
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    activeInput().focus()
    await user.keyboard('{Tab}{Tab}')
    paste('50000\t999\t888')

    // Neo ở "Đơn giá": ô thứ hai rơi vào "Thành tiền" (server tính), ô thứ ba
    // rơi ra ngoài bảng. Dồn chúng sang cột khác làm lệch cả vùng dán.
    expect(onCommit).toHaveBeenCalledExactlyOnceWith([
      { rowIndex: 0, columnKey: 'price', value: '50000' },
    ])
  })

  it('vùng dán dài hơn số dòng hiện có vẫn báo đủ — chỗ gọi tự nới dòng', () => {
    const onCommit = vi.fn()
    render(<Harness rowCount={2} onCommit={onCommit} />)

    activeInput().focus()
    paste('a\nb\nc\nd')

    const changes = onCommit.mock.calls[0]?.[0] as readonly DataGridChange[]
    expect(changes).toHaveLength(4)
    expect(changes[3]).toEqual({ rowIndex: 3, columnKey: 'item', value: 'd' })
    // Lưới không tự đẻ dòng: nó không biết một dòng chứng từ trống gồm những gì.
    expect(screen.getAllByRole('row')).toHaveLength(5)
  })

  it('dán một ô đơn để nguyên cho trình duyệt — chèn tại con trỏ như mọi ô nhập', () => {
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    activeInput().focus()
    paste('chỉ một ô')

    expect(onCommit).not.toHaveBeenCalled()
  })
})

describe('DataGrid — hợp đồng với chỗ gọi', () => {
  it('`onActiveCellChange` chỉ bắn khi ô ĐỔI, không phải mỗi lượt vẽ', async () => {
    const user = userEvent.setup()
    const onActiveCellChange = vi.fn()
    // Truyền dạng arrow inline — cách dùng mặc định, và cũng là cách làm prop
    // này đổi identity mỗi lượt vẽ. Nếu nó nằm trong mảng phụ thuộc của effect
    // thì chỗ gọi làm đúng thứ prop này sinh ra để làm (cập nhật thanh trạng
    // thái "dòng 12/500") sẽ vẽ lại → effect chạy lại → vòng lặp vẽ không dừng.
    render(
      <Harness
        commitMode="live"
        onActiveCellChange={(cell) => {
          onActiveCellChange(cell)
        }}
      />,
    )
    onActiveCellChange.mockClear()

    activeInput().focus()
    await user.keyboard('abcde')
    expect(onActiveCellChange).not.toHaveBeenCalled()

    await user.keyboard('{Tab}')
    expect(onActiveCellChange).toHaveBeenCalledExactlyOnceWith({ rowIndex: 0, columnIndex: 1 })
  })

  it('bớt cột lúc chạy vẫn còn ô nhập — bàn phím không chết', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<Harness />)

    activeInput().focus()
    await user.keyboard('{Tab}{Tab}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Đơn giá, dòng 1')

    // Ẩn/hiện cột thuế, đổi loại chứng từ, đổi mẫu in — đều là đổi `columns`
    // lúc chạy. Không kẹp ô đang chọn thì không ô nào khớp, không còn `<input>`,
    // và không còn `<input>` nghĩa là không còn `keydown`: chỉ chuột cứu được.
    rerender(<Harness columns={COLUMNS.slice(0, 1)} />)

    // Ô đang chọn bị kẹp về cột còn lại, và quan trọng nhất: VẪN CÒN một ô nhập
    // để bấm vào và gõ tiếp.
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 1')

    // Focus KHÔNG tự về (ô nhập là một node DOM khác) — người dùng bấm một lần
    // rồi làm việc tiếp. Ghi thành phép khẳng định để lần sau ai sửa cũng biết
    // đây là hành vi đã biết, không phải chỗ chưa ai để ý.
    await user.click(activeInput())
    await user.keyboard('{ArrowDown}')
    expect(activeInput()).toHaveAttribute('aria-label', 'Hàng hóa, dòng 2')
  })

  it('`aria-rowindex` theo chỉ số DỮ LIỆU, không theo vị trí trong DOM', async () => {
    const { container } = render(<Harness rowCount={500} />)

    await scrollTo(container, 30 * 200)

    // Cuộn ảo làm chỉ số DOM không liên tục. Lấy chỉ số DOM thì trình đọc màn
    // hình đọc "dòng 3 trên 501" khi người dùng đang ở dòng 203 — con số vô
    // nghĩa, và không có gì trên màn hình cho thấy nó sai.
    const rows = screen.getAllByRole('row')
    const first = rows[1]
    expect(first).toHaveAttribute('aria-rowindex')
    expect(Number(first?.getAttribute('aria-rowindex'))).toBeGreaterThan(100)
  })

  it('dán đúng MỘT ô trống là thao tác hợp lệ — xóa nội dung ô', () => {
    const onCommit = vi.fn()
    render(<Harness onCommit={onCommit} />)

    activeInput().focus()
    fireEvent.paste(activeInput(), { clipboardData: { getData: () => '' } })

    // Chuỗi rỗng không có gì để dán; để trình duyệt xử lý như dán chữ bình thường.
    expect(onCommit).not.toHaveBeenCalled()
  })

  it('lưới toàn cột chỉ-đọc thì không sinh ô nhập nào', () => {
    render(<Harness columns={[COLUMNS[3] as DataGridColumn<Line>]} />)

    // Ô đang chọn rơi vào cột chỉ-đọc (không còn cột sửa được nào). Vẫn không
    // được mở ô nhập ở đó: "Thành tiền" do server tính (H15/LD-03).
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
  })
})
