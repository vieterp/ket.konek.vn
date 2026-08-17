/**
 * Lưới nhập liệu — xem `data-grid.tsx` để biết ba quyết định làm nên hiệu năng
 * của nó (H11 một ô nhập · H12 không controlled · H15 không tính tiền).
 *
 * `parseClipboardTable` xuất ra ngoài vì màn hình nghiệp vụ nào có đường "nhập
 * từ Excel" riêng cũng cần đúng bộ quy tắc tách ô đó — hai bản cài quy tắc TSV
 * khác nhau trong cùng một ứng dụng là hai cách hiểu khác nhau về dữ liệu khách
 * hàng dán vào.
 */

export { parseClipboardTable } from './clipboard-tsv'
export { DataGrid } from './data-grid'
export type {
  DataGridAlign,
  DataGridCell,
  DataGridChange,
  DataGridColumn,
  DataGridCommitMode,
  DataGridProps,
} from './types'
