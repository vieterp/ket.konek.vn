/**
 * Bộ component cơ sở của design system Konek.
 *
 * Màn hình nghiệp vụ (`src/features/*`) nhập từ đây, **không** tự định nghĩa
 * lại nút/ô nhập/bảng riêng (docs/design-guidelines.md §5).
 *
 * **Bất biến của tầng này: component design system không phụ thuộc `src/lib/`.**
 * Không gọi `useI18n`, không gọi `apiClient`, không dùng router. Mọi chữ hiện
 * trên màn hình đi vào bằng prop. Ba cái lợi, cả ba đều đã dùng tới:
 * component vẽ được trong bài test không cần dựng provider; trang duyệt
 * `/kitchen-sink` mở được khi chưa đăng nhập và chưa có server; và cùng bộ
 * component này còn phải dùng lại cho mẫu in render ở server (ADR-009), nơi
 * không có ngữ cảnh React nào cả.
 *
 * `DataTable` là bảng **chỉ-đọc**; lưới **nhập liệu** nhiều dòng là `DataGrid`
 * (thư mục `data-grid/`). Hai component vì hai bài toán — xem quyết định H7.
 */

export { Alert } from './alert'
export type { AlertTone } from './alert'
export { Button } from './button'
export type { ButtonProps, ButtonVariant } from './button'
export { ChecklistPanel } from './checklist-panel'
export type { ChecklistItem, ChecklistPanelProps, ChecklistStatus } from './checklist-panel'
export { DataTable } from './data-table'
export type {
  ColumnAlign,
  DataTableColumn,
  DataTableProps,
  DataTableSort,
  DataTableTotalRow,
  SortDirection,
} from './data-table'
export { DataGrid, parseClipboardTable } from './data-grid'
export type {
  DataGridAlign,
  DataGridCell,
  DataGridChange,
  DataGridColumn,
  DataGridCommitMode,
  DataGridProps,
} from './data-grid'
export { Drawer } from './drawer'
export type { DrawerProps } from './drawer'
export { NextActionCell } from './next-action-cell'
export type { NextActionCellProps } from './next-action-cell'
export { Seg } from './seg'
export type { SegOption, SegProps } from './seg'
export { SelectField } from './select-field'
export type { SelectFieldProps, SelectOption } from './select-field'
export { SplitPane } from './split-pane'
export type { SplitPaneProps } from './split-pane'
export { StatusPill } from './status-pill'
export type { StatusPillProps, StatusTone } from './status-pill'
export { Tabs } from './tabs'
export type { TabItem, TabsProps } from './tabs'
export { TextField } from './text-field'
export type { TextFieldProps } from './text-field'
