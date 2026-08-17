/**
 * Hợp đồng của lưới nhập liệu.
 *
 * Tách khỏi `data-grid.tsx` vì trang đo hiệu năng, bài test và các màn hình
 * nghiệp vụ đều nhập kiểu mà không cần kéo theo cả component.
 */

import type { ReactNode } from 'react'

export type DataGridAlign = 'left' | 'right'

export interface DataGridColumn<Row> {
  readonly key: string
  readonly header: string
  /** `right` cho mọi cột số/tiền — xem `data-table.tsx` §1. Mặc định `left`. */
  readonly align?: DataGridAlign
  /**
   * Ô không sửa được: cột do **server** tính ("Thành tiền", "Tiền thuế").
   * Điều hướng bàn phím bỏ qua cột này, và dán cũng không ghi vào (H15).
   */
  readonly readOnly?: boolean
  /** Bề rộng cột tính bằng px. Không khai thì cột chia đều phần còn lại. */
  readonly width?: number
  /**
   * Giá trị **thô** để sửa: đúng chuỗi người dùng gõ và đúng chuỗi gửi lên
   * server. Không định dạng ở đây.
   */
  readonly value: (row: Row) => string
  /**
   * Giá trị hiện khi ô KHÔNG được chọn — chỗ định dạng nghìn/ngày. Không khai
   * thì hiện `value`.
   *
   * Hai hàm tách nhau vì ô đang sửa phải cho ra thứ gõ được: "12.500.000" là
   * thứ để đọc, "12500000" là thứ để sửa và để gửi đi.
   */
  readonly display?: (row: Row) => ReactNode
  /** Gợi ý bàn phím cho thiết bị cảm ứng. `decimal` cho cột tiền. */
  readonly inputMode?: 'text' | 'numeric' | 'decimal'
}

/**
 * Một ô đổi giá trị.
 *
 * `rowIndex` **được phép vượt quá** số dòng hiện có: dán 200 dòng vào một lưới
 * còn 3 dòng trống là thao tác bình thường của người nhập liệu. Chỗ gọi tự
 * quyết định nới thêm dòng hay cắt bớt vùng dán — lưới không tự đẻ dòng vì nó
 * không biết một dòng chứng từ trống phải gồm những gì.
 */
export interface DataGridChange {
  readonly rowIndex: number
  readonly columnKey: string
  readonly value: string
}

/** Ô đang được chọn. */
export interface DataGridCell {
  readonly rowIndex: number
  readonly columnIndex: number
}

/**
 * Thời điểm giá trị ô đi lên chỗ gọi.
 *
 * - `on-leave` (mặc định): khi rời ô (Tab/Enter/mũi tên/click ra ngoài). Trong
 *   lúc gõ, React **không** làm gì cả — độ trễ phím bằng đúng độ trễ của một
 *   `<input>` trần, không phụ thuộc lưới có 20 hay 5.000 dòng.
 * - `live`: mỗi ký tự. Dùng cho màn hình phải hiện số cộng dồn ngay khi gõ
 *   (bảng lương — nguyên tắc U9). Đắt hơn hẳn: mỗi phím kéo theo một lượt vẽ
 *   lại cửa sổ dòng. Trang đo `/bench/data-grid` đo đúng chênh lệch này.
 *
 * Cả hai chế độ đều **không** đụng vào ô đang gõ giữa chừng một tổ hợp IME
 * (quyết định H12).
 */
export type DataGridCommitMode = 'on-leave' | 'live'

export interface DataGridProps<Row> {
  readonly columns: readonly DataGridColumn<Row>[]
  readonly rows: readonly Row[]
  /**
   * Định danh **thật** của bản ghi — id, uid, mã dòng. **Không phải chỉ số dòng.**
   *
   * Đây không chỉ là khóa vẽ lại cho React: nó là hàng rào cho ca chỗ gọi đổi
   * dữ liệu dưới chân ô đang gõ (xóa dòng bằng phím tắt, dữ liệu về từ server).
   * Khóa đổi thì dòng và ô nhập được dựng lại, chữ chưa cam kết bị bỏ — đúng
   * thứ phải xảy ra, vì `rowIndex` giờ trỏ sang chứng từ khác. Truyền
   * `(_, index) => String(index)` làm khóa thì chữ của người dùng sẽ được ghi
   * sang một dòng khác mà không có gì báo.
   */
  readonly rowKey: (row: Row, index: number) => string
  /** Nhãn lưới cho trình đọc màn hình — bắt buộc. */
  readonly caption: string
  /**
   * Nhãn của ô đang sửa cho trình đọc màn hình, vd `(h, n) => \`${h}, dòng ${n}\``.
   *
   * Đi vào bằng prop chứ không ghép chuỗi trong component: tầng design system
   * không được chứa chữ của một ngôn ngữ nào (docs/design-guidelines.md §5).
   * Không khai thì nhãn chỉ còn tên cột — đọc được, nhưng người dùng trình đọc
   * màn hình mất số dòng.
   */
  readonly cellLabel?: (columnHeader: string, rowNumber: number) => string
  /** Nhận mọi thay đổi: gõ một ô = 1 phần tử, dán = cả vùng trong một lượt. */
  readonly onCommit: (changes: readonly DataGridChange[]) => void
  readonly commitMode?: DataGridCommitMode
  /** Chiều cao một dòng (px). Phải khớp CSS — cuộn ảo tính theo con số này. */
  readonly rowHeight?: number
  /** Chiều cao vùng cuộn (px). */
  readonly height?: number
  /**
   * Báo ô đang chọn cho thanh trạng thái ("dòng 12/500"). Ô đang chọn là trạng
   * thái **của lưới**, không phải của chỗ gọi: nếu đẩy nó lên trên thì mỗi phím
   * mũi tên thành một lượt vẽ lại từ gốc cây — đúng thứ H11 sinh ra để tránh.
   */
  readonly onActiveCellChange?: (cell: DataGridCell) => void
}
