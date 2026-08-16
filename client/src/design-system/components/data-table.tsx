/**
 * Bảng danh sách **chỉ-đọc** — nền của mọi màn hình liệt kê.
 *
 * Đây KHÔNG phải lưới nhập liệu. Lưới nhập chứng từ nhiều dòng (bàn phím, dán
 * từ Excel, IME tiếng Việt, 500 dòng) là bài toán khác hẳn và do spike S3 quyết
 * nền công nghệ — xem lát 2C-3, quyết định H7. Trộn hai thứ vào một component
 * là cách chắc chắn nhất để cả hai đều làm dở.
 *
 * Ba điều đáng nói:
 *
 * 1. **Số căn phải + `tabular-nums`.** Cột tiền căn trái thì mắt không so được
 *    hàng đơn vị giữa các dòng, và font tỉ lệ làm chữ số rộng hẹp khác nhau nên
 *    các cột số lệch nhau. Với phần mềm kế toán đây là lỗi đọc số, không phải
 *    lỗi thẩm mỹ. Khai `align: 'right'` là cách duy nhất — không tự đoán theo
 *    kiểu dữ liệu, vì "mã số thuế" cũng toàn chữ số mà phải căn trái.
 * 2. **Sắp xếp do chỗ gọi điều khiển.** Danh sách thật lấy theo trang từ server,
 *    nên sắp xếp tại chỗ chỉ sắp đúng trang đang xem — đúng loại sai âm thầm.
 *    Component chỉ báo "người dùng bấm cột này"; ai sắp là việc của chỗ gọi.
 * 3. **Không chọn dòng bằng cách bấm cả dòng.** Muốn mở chi tiết thì đặt một
 *    nút thật vào một cột (`NextActionCell` sinh ra để làm đúng việc đó): một
 *    `<tr>` bấm được không có ngữ nghĩa cho trình đọc màn hình, không dùng được
 *    bằng bàn phím nếu không tự vá, và trên máy tính bảng thì mọi thao tác cuộn
 *    đều có nguy cơ mở nhầm.
 *
 * 4. **Dòng tổng là hợp đồng riêng (`totals`), không phải dòng dữ liệu có cờ.**
 *    Bảng kế toán nào cũng có "Số dư đầu tháng", "Cộng phát sinh", "A · TÀI SẢN
 *    NGẮN HẠN" — và chúng không được tham gia sắp xếp (phải nằm đúng chỗ dù
 *    người dùng sắp cột nào) cũng không được client tự cộng ra.
 *
 * Không có phép tính nào ở đây — `render` và `totals` nhận số đã tính sẵn từ
 * server và chỉ định dạng (docs/design-guidelines.md §5).
 */

import type { ReactElement, ReactNode } from 'react'

export type ColumnAlign = 'left' | 'right'

export type SortDirection = 'asc' | 'desc'

export interface DataTableColumn<Row> {
  readonly key: string
  readonly header: string
  /** `right` cho mọi cột số/tiền. Mặc định `left`. */
  readonly align?: ColumnAlign
  readonly sortable?: boolean
  readonly render: (row: Row) => ReactNode
}

export interface DataTableSort {
  readonly key: string
  readonly direction: SortDirection
}

/**
 * Dòng tổng (`tr.tot` trong design) — "Số dư đầu tháng", "Cộng phát sinh
 * tháng", "A · TÀI SẢN NGẮN HẠN".
 *
 * Là hợp đồng riêng chứ không phải một dòng dữ liệu có cờ, vì hai lý do: nó
 * **không** tham gia sắp xếp hay lọc (dòng tổng phải nằm đúng chỗ của nó dù
 * người dùng sắp cột nào), và số trên đó do **server** tính — client không
 * được cộng lại cột tiền (LD-03, docs/design-guidelines.md §5).
 *
 * `cells` khóa theo `column.key` nên dòng tổng không thể lệch cột khi thứ tự
 * cột đổi; ô nào không có giá trị thì để trống.
 */
export interface DataTableTotalRow {
  readonly key: string
  readonly label: string
  readonly cells: Readonly<Record<string, ReactNode>>
  /** `above` cho "Số dư đầu kỳ"; mặc định nằm dưới các dòng dữ liệu. */
  readonly position?: 'above' | 'below'
}

export interface DataTableProps<Row> {
  readonly columns: readonly DataTableColumn<Row>[]
  readonly rows: readonly Row[]
  readonly rowKey: (row: Row) => string
  /** Nhãn bảng cho trình đọc màn hình — bắt buộc, bảng nào cũng phải tự giới thiệu. */
  readonly caption: string
  readonly emptyLabel: string
  readonly loading?: boolean
  readonly loadingLabel?: string
  readonly sort?: DataTableSort
  readonly onSortChange?: (sort: DataTableSort) => void
  /** Dòng tổng do server tính. Không tham gia sắp xếp. */
  readonly totals?: readonly DataTableTotalRow[]
  /** Tô nền dòng chẵn. Bật cho bảng dài (sổ cái, bảng kê); tắt cho bảng ngắn. */
  readonly zebra?: boolean
}

const ALIGN: Record<ColumnAlign, string> = {
  left: 'text-left',
  // `tabular-nums` ép mọi chữ số rộng bằng nhau nên các cột số thẳng hàng.
  // `whitespace-nowrap` để một số tiền không bao giờ bị ngắt làm hai dòng —
  // "12.500.000,00" gãy giữa chừng là một con số đọc sai.
  right: 'text-right tabular-nums whitespace-nowrap',
}

/** Bấm lại cột đang sắp thì đảo chiều; bấm cột khác thì bắt đầu từ tăng dần. */
function nextSort(current: DataTableSort | undefined, key: string): DataTableSort {
  if (current?.key === key && current.direction === 'asc') {
    return { key, direction: 'desc' }
  }
  return { key, direction: 'asc' }
}

export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  caption,
  emptyLabel,
  loading = false,
  loadingLabel,
  sort,
  onSortChange,
  totals = [],
  zebra = false,
}: DataTableProps<Row>): ReactElement {
  const message = loading ? (loadingLabel ?? emptyLabel) : emptyLabel
  const showMessage = loading || rows.length === 0

  function renderTotal(total: DataTableTotalRow): ReactElement {
    return (
      <tr key={total.key} data-total className="border-b border-border-default bg-surface last:border-b-0">
        {columns.map((column, index) => {
          const className = `px-3 py-2 font-bold text-primary ${ALIGN[column.align ?? 'left']}`
          // Nhãn dòng tổng ("Cộng phát sinh tháng") là tiêu đề của cả hàng, nên
          // nó phải là `<th scope="row">` chứ không phải `<td>`: trình đọc màn
          // hình mới đọc "Cộng phát sinh tháng, 1.750.000" thay vì đọc trơ một
          // con số không biết của cái gì.
          return index === 0 ? (
            <th key={column.key} scope="row" className={className}>
              {total.label}
            </th>
          ) : (
            <td key={column.key} className={className}>
              {total.cells[column.key] ?? null}
            </td>
          )
        })}
      </tr>
    )
  }

  const above = totals.filter((total) => total.position === 'above')
  const below = totals.filter((total) => total.position !== 'above')

  return (
    <div className="overflow-x-auto rounded border border-border-default bg-background">
      <table className="w-full border-collapse text-app">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border-default bg-screen">
            {columns.map((column) => {
              const align = column.align ?? 'left'
              const sorted = sort?.key === column.key
              const canSort = column.sortable === true && onSortChange !== undefined
              return (
                <th
                  key={column.key}
                  scope="col"
                  // `aria-sort` chỉ được đặt trên cột ĐANG sắp; đặt "none" khắp
                  // nơi làm trình đọc màn hình đọc thừa ở mọi cột.
                  aria-sort={sorted ? (sort.direction === 'asc' ? 'ascending' : 'descending') : undefined}
                  className={`px-3 py-2 text-meta font-semibold text-text-default ${ALIGN[align]}`}
                >
                  {canSort ? (
                    <button
                      type="button"
                      onClick={() => {
                        onSortChange(nextSort(sort, column.key))
                      }}
                      className="inline-flex items-center gap-1 font-semibold text-text-default hover:text-primary"
                    >
                      {column.header}
                      <span aria-hidden className="text-xs text-text-muted">
                        {sorted ? (sort.direction === 'asc' ? '▲' : '▼') : '↕'}
                      </span>
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              )
            })}
          </tr>
        </thead>
        <tbody>
          {showMessage ? (
            <tr>
              <td
                colSpan={columns.length}
                className="px-3 py-8 text-center text-app text-text-muted"
              >
                {message}
              </td>
            </tr>
          ) : (
            <>
              {above.map(renderTotal)}
              {rows.map((row, index) => (
                <tr
                  key={rowKey(row)}
                  className={`border-b border-border-default last:border-b-0 ${
                    zebra && index % 2 === 1 ? 'bg-row-alt' : ''
                  }`}
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={`px-3 py-2 text-text-default ${ALIGN[column.align ?? 'left']}`}
                    >
                      {column.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
              {below.map(renderTotal)}
            </>
          )}
        </tbody>
      </table>
    </div>
  )
}
