/**
 * Danh sách nhóm màn hình của sidebar.
 *
 * Thứ tự và cách gộp lấy từ design Konek Screens 2a, **không** theo ranh giới
 * module backend (docs/design-guidelines.md §3). Ví dụ: "Tiền vào tiền ra" là
 * một mục ở đây nhưng là hai module `cash_book` + `bank` ở server; "Tài sản" là
 * một mục nhưng là `fixed_assets` + `tools`.
 *
 * `permission` là mã quyền tối thiểu để **thấy** mục. Ẩn menu chỉ là phép lịch
 * sự với người dùng — cửa thật vẫn khóa ở server (`require_permission`), và
 * mọi mục ở lát này chưa có endpoint nào nên chúng chưa khai quyền.
 */

import type { LucideIcon } from 'lucide-react'
import {
  Archive,
  Banknote,
  BookOpenCheck,
  Building2,
  FileText,
  LayoutDashboard,
  Receipt,
  ShoppingCart,
  Store,
  Users,
} from 'lucide-react'

import type { TranslationKey } from '@/locales/vi'

export interface NavigationItem {
  /** Đường dẫn — tiếng Việt không dấu, trùng tên thư mục `src/features/*`. */
  readonly path: string
  readonly labelKey: TranslationKey
  readonly icon: LucideIcon
  /** Phase của kế hoạch sở hữu nhóm màn hình này (hiện trên trang tạm). */
  readonly phase: string
}

export const NAVIGATION: readonly NavigationItem[] = [
  { path: '/', labelKey: 'nav.home', icon: LayoutDashboard, phase: '2' },
  {
    path: '/tien-vao-tien-ra',
    labelKey: 'nav.tien-vao-tien-ra',
    icon: Banknote,
    phase: '6',
  },
  { path: '/mua-hang', labelKey: 'nav.mua-hang', icon: ShoppingCart, phase: '7' },
  { path: '/ban-hang', labelKey: 'nav.ban-hang', icon: Store, phase: '7' },
  { path: '/hoa-don-dien-tu', labelKey: 'nav.hoa-don-dien-tu', icon: Receipt, phase: '7' },
  { path: '/kho', labelKey: 'nav.kho', icon: Archive, phase: '8' },
  { path: '/tai-san', labelKey: 'nav.tai-san', icon: Building2, phase: '8' },
  { path: '/luong', labelKey: 'nav.luong', icon: Users, phase: '9' },
  { path: '/so-sach-thue', labelKey: 'nav.so-sach-thue', icon: BookOpenCheck, phase: '10a' },
  {
    path: '/danh-muc-thiet-lap',
    labelKey: 'nav.danh-muc-thiet-lap',
    icon: FileText,
    phase: '3',
  },
]
