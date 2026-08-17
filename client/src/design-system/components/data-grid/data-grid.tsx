/**
 * Lưới **nhập liệu** nhiều dòng — chứng từ bán hàng, phiếu nhập kho, bảng lương.
 *
 * Không phải bản "sửa được" của `DataTable`. `DataTable` là bảng chỉ-đọc và trả
 * lời câu hỏi "có gì trong danh sách"; cái này là chỗ người kế toán ngồi gõ cả
 * ngày và trả lời câu hỏi "gõ có kịp tay không". Hai bài toán khác nhau nên là
 * hai component (quyết định H7).
 *
 * Ngưỡng phải đạt (spike S3, phase 2 bước 17): 500 dòng, độ trễ phím < 50ms,
 * dán 200 dòng từ Excel < 1s, gõ tiếng Việt liên tục không mất ký tự. Ba quyết
 * định dưới đây là lời đáp, và cả ba đều có bài test khóa lại:
 *
 * **H11 — đúng MỘT `<input>` tồn tại tại một thời điểm.** Ô không được chọn chỉ
 * là chữ. Lưới 500 × 8 dựng đủ ô nhập là 4.000 node DOM có trạng thái riêng;
 * mỗi phím gõ, trình duyệt và React đều phải đi qua từng ấy thứ. Mọi lưới trên
 * desktop đều làm như vậy — đây không phải mẹo tối ưu mà là mô hình đúng.
 * Nửa còn lại là cuộn ảo, xem `use-row-window.ts`.
 *
 * **H12 — ô đang gõ KHÔNG controlled.** `<input>` giữ giá trị của chính nó;
 * giá trị chỉ đi lên chỗ gọi khi rời ô. Hai cái lợi, cái thứ hai mới là lý do
 * thật: (1) gõ không kéo theo lượt vẽ lại nào, nên độ trễ phím bằng đúng độ trễ
 * của một `<input>` trần; (2) **IME tiếng Việt không bị cắt ngang**. Telex gõ
 * "được" là một tổ hợp `compositionstart → update× → compositionend`; React ghi
 * đè `value` giữa chừng là nuốt dấu, và triệu chứng của nó — thỉnh thoảng mất
 * một chữ — là loại lỗi không ai chụp màn hình lại được để báo.
 *
 * Cùng lý do đó, mọi phím **bỏ qua khi đang có tổ hợp IME**: phím Enter kết
 * thúc một tổ hợp Telex và phím Enter xuống dòng dưới là cùng một sự kiện
 * `keydown`; không phân biệt thì gõ "phải" xong là nhảy mất dòng.
 *
 * **H15 — lưới không tính tiền.** Giá trị đi qua đây là **chuỗi**, đúng thứ
 * người dùng gõ và đúng thứ gửi lên server. Cột "Thành tiền" khai `readOnly` và
 * do server tính (LD-03). Đây là chỗ cám dỗ nhất trong toàn bộ client để lỡ
 * viết một phép nhân — và một phép nhân `number` trong JavaScript là một con số
 * sai trên báo cáo tài chính.
 *
 * Bất biến của tầng design system vẫn giữ: không import `src/lib/`, mọi chữ vào
 * bằng prop (docs/design-guidelines.md §5).
 */

import type {
  ClipboardEvent as ReactClipboardEvent,
  KeyboardEvent as ReactKeyboardEvent,
  ReactElement,
} from 'react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { parseClipboardTable } from './clipboard-tsv'
import type { DataGridCell, DataGridChange, DataGridProps } from './types'
import { useRowWindow } from './use-row-window'

/** Ô nhập được kéo vào tầm nhìn rồi làm gì tiếp. */
type FocusIntent = 'none' | 'select' | 'caret'

const CELL_BASE = 'flex items-center overflow-hidden px-2 text-app'

export function DataGrid<Row>({
  columns,
  rows,
  rowKey,
  caption,
  cellLabel,
  onCommit,
  commitMode = 'on-leave',
  rowHeight = 30,
  height = 420,
  onActiveCellChange,
}: DataGridProps<Row>): ReactElement {
  const editableColumns = columns
    .map((column, index) => (column.readOnly === true ? -1 : index))
    .filter((index) => index >= 0)

  const [rawActive, setActive] = useState<DataGridCell>({
    rowIndex: 0,
    columnIndex: editableColumns[0] ?? 0,
  })
  // Ô nhập không controlled nên phải dựng lại nó khi giá trị nền đổi mà không
  // do người dùng gõ — dán một vùng đè lên chính ô đang chọn là ca thật.
  const [remountToken, setRemountToken] = useState(0)

  // Kẹp ô đang chọn vào phạm vi dữ liệu HIỆN TẠI, ngay lúc vẽ.
  //
  // Chỗ gọi đổi `columns` lúc chạy là việc bình thường — ẩn/hiện cột thuế, đổi
  // loại chứng từ, đổi mẫu in. Không kẹp thì `active.columnIndex` trỏ ra ngoài
  // mảng, không ô nào khớp `isActive`, **không còn `<input>` nào** — và không
  // còn `<input>` nghĩa là không còn `keydown`: bàn phím chết hẳn, chỉ chuột
  // cứu được. Kẹp ở đây chứ không trong một effect vì effect chạy sau lượt vẽ,
  // tức là đã có một khung hình không bàn phím rồi.
  const active: DataGridCell = {
    rowIndex: Math.min(Math.max(0, rawActive.rowIndex), Math.max(0, rows.length - 1)),
    columnIndex: Math.min(Math.max(0, rawActive.columnIndex), Math.max(0, columns.length - 1)),
  }

  /**
   * Node của ô nhập — **không bao giờ đặt lại về `null`** khi React tháo nó.
   *
   * Một node đã rời khỏi cây DOM vẫn đọc được `.value`, và đó chính là thứ cần
   * cứu: khi dòng đang gõ rời cửa sổ cuộn, ô nhập bị tháo mà trình duyệt
   * **không** bắn `blur` — không giữ lại node thì chữ người dùng vừa gõ biến
   * mất không dấu vết. `liveInput()` dùng cho những việc chỉ có nghĩa với node
   * còn gắn trong trang (đặt focus).
   */
  const inputRef = useRef<HTMLInputElement | null>(null)
  const focusIntent = useRef<FocusIntent>('none')
  const composing = useRef(false)
  // Ảnh chụp cho những chỗ chạy NGOÀI lượt vẽ (trình xử lý sự kiện, effect).
  const latest = useRef({ active, columnKey: '', onCommit })
  const activeCallback = useRef(onActiveCellChange)

  function liveInput(): HTMLInputElement | null {
    const node = inputRef.current
    return node !== null && node.isConnected ? node : null
  }

  /**
   * Giá trị ô lúc **vào ô** (mốc để `Escape` hoàn tác) và giá trị **đã cam kết
   * gần nhất** (mốc chống cam kết trùng).
   *
   * Hai mốc khác nhau, và trước đây chúng là một — đó là lỗi. Ở chế độ `live`,
   * mỗi phím gõ là một lần cam kết, nên nếu `Escape` hoàn tác về "giá trị đã
   * cam kết gần nhất" thì nó hoàn tác về đúng thứ vừa gõ, tức là **không làm
   * gì**. Còn nếu mốc chống-trùng bị đặt lại ở mọi lượt vẽ (nó từng bị) thì một
   * chỗ gọi lưu chậm — không phản chiếu `onCommit` lại ngay — sẽ nhận **cùng
   * một thay đổi hai lần** mỗi lần rời ô.
   */
  const baseline = useRef('')
  const lastCommitted = useRef('')

  const { scrollRef, rowWindow, onScroll, scrollRowIntoView } = useRowWindow({
    rowCount: rows.length,
    rowHeight,
    height,
  })

  const activeColumn = columns[active.columnIndex]
  const { rowIndex: activeRowIndex, columnIndex: activeColumnIndex } = active

  // Ảnh chụp phải mới trước khi effect nào ở dưới đọc nó, nên đây là layout
  // effect và nó khai TRƯỚC — React chạy layout effect theo thứ tự khai báo.
  useLayoutEffect(() => {
    latest.current = { active, columnKey: activeColumn?.key ?? '', onCommit }
    activeCallback.current = onActiveCellChange
  })

  // Đặt lại hai mốc khi và chỉ khi VÀO một ô khác (hoặc ô bị dựng lại vì dán
  // đè). **Không** phụ thuộc giá trị của ô: ở chế độ `live` nó đổi theo từng
  // phím, mà mốc hoàn tác của `Escape` thì phải đứng yên suốt thời gian ở trong
  // ô. Cùng chỗ này đặt lại cờ IME — đổi ô giữa một tổ hợp Telex bằng chuột
  // cũng làm `compositionend` không bao giờ bắn.
  useLayoutEffect(() => {
    baseline.current = liveInput()?.defaultValue ?? ''
    lastCommitted.current = baseline.current
    composing.current = false
  }, [activeRowIndex, activeColumnIndex, remountToken])

  useEffect(() => {
    // Gọi qua ref: prop này gần như luôn được truyền dạng arrow inline, nên nếu
    // nó nằm trong mảng phụ thuộc thì effect chạy ở MỌI lượt vẽ. Chỗ gọi làm
    // đúng thứ prop này sinh ra để làm — cập nhật thanh trạng thái "dòng
    // 12/500" — sẽ vẽ lại, effect lại chạy, và vòng lặp vẽ không dừng (đo
    // được: tiến trình Node hết bộ nhớ).
    activeCallback.current?.({ rowIndex: activeRowIndex, columnIndex: activeColumnIndex })
  }, [activeRowIndex, activeColumnIndex])

  useEffect(() => {
    if (focusIntent.current === 'none') {
      return
    }
    const intent = focusIntent.current
    focusIntent.current = 'none'
    const input = liveInput()
    if (input === null) {
      return
    }
    input.focus()
    if (intent === 'select') {
      // Đến ô bằng bàn phím thì chọn sẵn cả nội dung: người nhập liệu Tab qua
      // một cột rồi gõ đè là thao tác thường xuyên nhất, và phải bôi đen tay
      // trước mỗi ô thì lưới coi như không dùng được bằng bàn phím.
      input.select()
    }
  }, [activeRowIndex, activeColumnIndex, remountToken])

  /**
   * Đẩy giá trị ô đang gõ lên chỗ gọi.
   *
   * Gọi lại nhiều lần không sao: sau lượt đầu, `lastCommitted` đã bằng giá trị
   * hiện tại nên các lượt sau không làm gì. Cần thế vì "rời ô" đến từ **bốn**
   * đường chồng lên nhau: bàn phím, `blur`, ô bị cuộn ra khỏi cửa sổ dòng, và
   * chỗ gọi đổi `rows`/`columns` dưới chân ô đang gõ.
   *
   * `element` truyền vào cho trường hợp node đã rời cây DOM — lúc đó
   * `liveInput()` trả `null` nhưng `.value` trên node cũ vẫn đọc được.
   *
   * **`rowKey` phải là định danh THẬT của bản ghi, không phải chỉ số dòng.**
   * Đó là cách lưới biết dữ liệu đã đổi dưới chân ô đang gõ: chỗ gọi xóa/lọc/
   * tải lại dòng thì khóa ở vị trí ấy đổi theo, React dựng lại dòng và ô nhập,
   * và chữ chưa cam kết bị bỏ — đúng thứ phải xảy ra, vì `rowIndex` giờ trỏ
   * sang chứng từ khác và ghi chữ của người dùng vào đó là hỏng sổ. Truyền
   * `(_, index) => String(index)` làm khóa thì mất hẳn hàng rào này.
   */
  function flushCell(element?: HTMLInputElement): void {
    const input = element ?? inputRef.current
    const snapshot = latest.current
    if (input === null || input === undefined || snapshot.columnKey === '') {
      return
    }
    if (input.value === lastCommitted.current) {
      return
    }
    lastCommitted.current = input.value
    snapshot.onCommit([
      { rowIndex: snapshot.active.rowIndex, columnKey: snapshot.columnKey, value: input.value },
    ])
  }

  /**
   * Cứu chữ đang gõ khi ô nhập bị tháo mà KHÔNG qua `blur`.
   *
   * Hai đường, cả hai đều im lặng nếu không có đoạn này:
   *
   * 1. **Dòng đang gõ rời cửa sổ cuộn.** Lăn chuột xuống xem tổng cộng ở cuối
   *    lưới 500 dòng là thao tác hằng ngày. Trình duyệt không bắn `blur` khi
   *    phần tử đang focus bị gỡ khỏi DOM, nên `onBlur` không chạy — đo được
   *    trên Chromium: 0 lượt cam kết, cuộn lên thì dòng trống trơn.
   * 2. **Chỗ gọi đổi bản ghi dưới chân ô đang gõ** (xóa dòng bằng phím tắt, dữ
   *    liệu về từ server). Ở đây thì ngược lại: **không được** chốt. `rowIndex`
   *    giờ trỏ sang chứng từ khác, mà ghi chữ của người dùng sang một dòng khác
   *    còn tệ hơn nhiều so với bỏ nó đi.
   *
   * `useLayoutEffect` chứ không `useEffect`: nó chạy ngay trong lượt commit đã
   * gỡ node, trước khi trình duyệt vẽ khung hình kế tiếp.
   */
  useLayoutEffect(() => {
    const node = inputRef.current
    if (node === null || node.isConnected) {
      return
    }
    flushCell(node)
    // Ô nhập biến mất giữa một tổ hợp IME thì `compositionend` không bao giờ
    // bắn. Không đặt lại cờ ở đây thì nó kẹt `true` vĩnh viễn: mọi phím của
    // lưới (Tab, Enter, mũi tên, Escape) chết hết trong khi chữ vẫn gõ được —
    // triệu chứng không ai đoán ra nguyên nhân.
    composing.current = false
  })

  function moveTo(cell: DataGridCell, intent: FocusIntent = 'select'): void {
    flushCell()
    focusIntent.current = intent
    setActive(cell)
    scrollRowIntoView(cell.rowIndex)
  }

  /**
   * Sang ô sửa được kế tiếp, vắt sang dòng khác khi hết cột.
   *
   * Trả `false` khi đã ở đầu/cuối lưới — lúc đó phím Tab phải được thả cho
   * trình duyệt xử lý, để người dùng ra được nút "Lưu" mà không phải với chuột.
   */
  function moveHorizontal(delta: number): boolean {
    if (editableColumns.length === 0) {
      return false
    }
    const position = editableColumns.indexOf(active.columnIndex)
    const next = position + delta
    if (next >= 0 && next < editableColumns.length) {
      moveTo({ rowIndex: active.rowIndex, columnIndex: editableColumns[next] ?? 0 })
      return true
    }
    const rowIndex = active.rowIndex + delta
    if (rowIndex < 0 || rowIndex >= rows.length) {
      return false
    }
    const columnIndex = delta > 0 ? editableColumns[0] : editableColumns[editableColumns.length - 1]
    moveTo({ rowIndex, columnIndex: columnIndex ?? 0 })
    return true
  }

  function moveVertical(delta: number): void {
    const rowIndex = active.rowIndex + delta
    if (rowIndex < 0 || rowIndex >= rows.length) {
      return
    }
    moveTo({ rowIndex, columnIndex: active.columnIndex })
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLInputElement>): void {
    // Đang trong một tổ hợp IME: mọi phím thuộc về bộ gõ, không thuộc về lưới.
    if (composing.current || event.nativeEvent.isComposing) {
      return
    }

    switch (event.key) {
      case 'Tab': {
        if (moveHorizontal(event.shiftKey ? -1 : 1)) {
          event.preventDefault()
        }
        return
      }
      case 'Enter': {
        // Chặn mặc định kể cả khi không đi đâu được: lưới hay nằm trong một
        // `<form>`, và Enter ở đó là gửi form — tức là ghi sổ một chứng từ dở.
        event.preventDefault()
        moveVertical(event.shiftKey ? -1 : 1)
        return
      }
      case 'ArrowDown': {
        event.preventDefault()
        moveVertical(1)
        return
      }
      case 'ArrowUp': {
        event.preventDefault()
        moveVertical(-1)
        return
      }
      case 'ArrowLeft':
      case 'ArrowRight': {
        // Mũi tên ngang trước hết là di chuyển con trỏ TRONG chữ; chỉ khi con
        // trỏ đã ở mép ô (và không đang bôi đen) thì mới nhảy sang ô khác. Sửa
        // một chữ số giữa "12500000" mà phải dùng chuột là hỏng hẳn.
        const input = event.currentTarget
        const caret = input.selectionStart
        const hasSelection = input.selectionStart !== input.selectionEnd
        const atEdge =
          !hasSelection &&
          caret !== null &&
          (event.key === 'ArrowLeft' ? caret === 0 : caret === input.value.length)
        if (atEdge && moveHorizontal(event.key === 'ArrowLeft' ? -1 : 1)) {
          event.preventDefault()
        }
        return
      }
      case 'Escape': {
        event.preventDefault()
        // Trả ô về giá trị lúc VÀO ô. Không dùng `setState` vì ô không
        // controlled — ghi thẳng vào DOM là đường duy nhất.
        event.currentTarget.value = baseline.current
        event.currentTarget.select()
        // Ở `live`, những phím đã gõ ĐÃ đi lên chỗ gọi rồi, nên hoàn tác phải
        // là một lần cam kết ngược — nếu không, Escape chỉ sửa cái người dùng
        // nhìn thấy còn dữ liệu thật vẫn mang con số gõ nhầm. Ở `on-leave`
        // chưa cam kết gì nên `flushCell` tự nhận ra và không làm gì.
        if (commitMode === 'live') {
          flushCell()
        }
        return
      }
      default:
    }
  }

  function handlePaste(event: ReactClipboardEvent<HTMLDivElement>): void {
    // Cùng hàng rào với bàn phím (H12), và nó phải viết riêng ở đây vì dán
    // không đi qua `handleKeyDown`. Dán giữa một tổ hợp Telex sẽ dựng lại ô
    // nhập và phá tổ hợp đang soạn — bộ gõ mất chỗ bám, chữ đang soạn rơi mất.
    if (composing.current) {
      return
    }
    const table = parseClipboardTable(event.clipboardData.getData('text/plain'))
    // Dán một ô đơn phải hành xử như dán chữ bình thường (chèn tại con trỏ,
    // đè phần đang bôi đen). Chỉ vùng nhiều ô mới là "dán bảng".
    if (table.length === 0 || (table.length === 1 && (table[0]?.length ?? 0) <= 1)) {
      return
    }
    event.preventDefault()

    const anchor = latest.current.active
    const changes: DataGridChange[] = []
    table.forEach((line, lineOffset) => {
      line.forEach((value, cellOffset) => {
        const column = columns[anchor.columnIndex + cellOffset]
        // Vùng dán rộng hơn số cột còn lại, hoặc trùm lên cột do server tính:
        // bỏ qua ô đó chứ không dồn sang cột kế bên — dồn cột làm lệch cả bảng
        // mà không có gì báo cho người dùng biết.
        if (column === undefined || column.readOnly === true) {
          return
        }
        changes.push({
          rowIndex: anchor.rowIndex + lineOffset,
          columnKey: column.key,
          value,
        })
      })
    })

    if (changes.length === 0) {
      return
    }
    latest.current.onCommit(changes)
    // Ô neo vừa bị đè bằng giá trị mới; ô nhập không controlled nên phải dựng
    // lại nó, nếu không người dùng vẫn thấy giá trị cũ trong đúng ô đang chọn.
    focusIntent.current = 'select'
    setRemountToken((token) => token + 1)
  }

  const gridTemplateColumns = columns
    .map((column) => (column.width === undefined ? 'minmax(0, 1fr)' : `${column.width}px`))
    .join(' ')

  const visibleRows = rows.slice(rowWindow.startIndex, rowWindow.endIndex)

  return (
    <div
      role="grid"
      aria-label={caption}
      aria-rowcount={rows.length + 1}
      aria-colcount={columns.length}
      className="overflow-hidden rounded border border-border-default bg-background"
    >
      <div
        role="row"
        aria-rowindex={1}
        className="grid border-b border-border-default bg-screen"
        style={{ gridTemplateColumns, height: rowHeight }}
      >
        {columns.map((column, columnIndex) => (
          <div
            key={column.key}
            role="columnheader"
            aria-colindex={columnIndex + 1}
            className={`${CELL_BASE} text-meta font-semibold text-text-default ${
              column.align === 'right' ? 'justify-end' : ''
            }`}
          >
            {column.header}
          </div>
        ))}
      </div>

      <div
        ref={scrollRef}
        onScroll={onScroll}
        onPaste={handlePaste}
        style={{ height }}
        className="overflow-auto"
      >
        <div
          role="rowgroup"
          style={{ paddingTop: rowWindow.topPad, paddingBottom: rowWindow.bottomPad }}
        >
          {visibleRows.map((row, offset) => {
            const rowIndex = rowWindow.startIndex + offset
            return (
              <div
                key={rowKey(row, rowIndex)}
                role="row"
                aria-rowindex={rowIndex + 2}
                className="grid border-b border-border-default last:border-b-0"
                style={{ gridTemplateColumns, height: rowHeight }}
              >
                {columns.map((column, columnIndex) => {
                  const isActive =
                    rowIndex === active.rowIndex && columnIndex === active.columnIndex
                  const alignRight = column.align === 'right'
                  return (
                    <div
                      key={column.key}
                      role="gridcell"
                      aria-colindex={columnIndex + 1}
                      aria-selected={isActive}
                      aria-readonly={column.readOnly === true ? true : undefined}
                      onMouseDown={(event) => {
                        // Đang ở đúng ô này rồi thì để yên cho trình duyệt đặt
                        // con trỏ vào chỗ vừa bấm.
                        if (column.readOnly === true || isActive) {
                          return
                        }
                        // Chặn mặc định để trình duyệt không tự chuyển focus
                        // sau khi React đã vẽ lại — nếu không, ô nhập mới vừa
                        // nhận focus lại bị lấy đi ngay trong cùng một sự kiện.
                        event.preventDefault()
                        moveTo({ rowIndex, columnIndex }, 'caret')
                      }}
                      className={`${CELL_BASE} border-r border-border-default last:border-r-0 ${
                        alignRight ? 'justify-end tabular-nums' : ''
                      } ${
                        column.readOnly === true
                          ? 'bg-surface text-text-muted'
                          : 'text-text-default'
                      } ${isActive ? 'outline outline-2 -outline-offset-2 outline-ocean-700' : ''}`}
                    >
                      {isActive && column.readOnly !== true ? (
                        <input
                          // Khóa gồm cả ô lẫn `remountToken`: đổi ô thì nạp giá
                          // trị nền của ô mới, còn `remountToken` là đường duy
                          // nhất để nạp lại khi chính ô này bị dán đè.
                          key={`${rowIndex}:${column.key}:${remountToken}`}
                          ref={(node) => {
                            // Cố ý **không** nhận `null`: node đã rời cây DOM
                            // vẫn đọc được `.value`, và layout effect ở trên
                            // dựa vào đúng điều đó để cứu chữ đang gõ khi dòng
                            // rời cửa sổ cuộn. `liveInput()` lọc ra node còn
                            // gắn trong trang cho những việc cần nó.
                            if (node !== null) {
                              inputRef.current = node
                            }
                          }}
                          defaultValue={column.value(row)}
                          inputMode={column.inputMode ?? 'text'}
                          aria-label={cellLabel?.(column.header, rowIndex + 1) ?? column.header}
                          onKeyDown={handleKeyDown}
                          onCompositionStart={() => {
                            composing.current = true
                          }}
                          onCompositionEnd={() => {
                            composing.current = false
                            if (commitMode === 'live') {
                              flushCell()
                            }
                          }}
                          onChange={() => {
                            // `on-leave` không làm gì ở đây — đó chính là chỗ
                            // độ trễ phím biến mất.
                            if (commitMode === 'live' && !composing.current) {
                              flushCell()
                            }
                          }}
                          onBlur={() => {
                            flushCell()
                          }}
                          className={`w-full bg-transparent text-app text-text-default outline-none ${
                            alignRight ? 'text-right tabular-nums' : ''
                          }`}
                        />
                      ) : (
                        <span className="truncate">
                          {column.display?.(row) ?? column.value(row)}
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
