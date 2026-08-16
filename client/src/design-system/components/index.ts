/**
 * Bộ component cơ sở của design system Konek.
 *
 * Màn hình nghiệp vụ (`src/features/*`) nhập từ đây, **không** tự định nghĩa
 * lại nút/ô nhập riêng (docs/design-guidelines.md §5).
 *
 * Lát 2C-1 chỉ dựng đúng những gì đường đăng nhập cần. Bộ đầy đủ — `Tabs`,
 * `DataGrid`, `Drawer`, `StatusPill`, `NextActionCell`, `SplitPane`,
 * `ChecklistPanel` — thuộc lát 2C-2 (bước 16), cùng trang duyệt trực quan để
 * review với người thiết kế.
 */

export { Alert } from './alert'
export type { AlertTone } from './alert'
export { Button } from './button'
export type { ButtonProps, ButtonVariant } from './button'
export { SelectField } from './select-field'
export type { SelectFieldProps, SelectOption } from './select-field'
export { TextField } from './text-field'
export type { TextFieldProps } from './text-field'
