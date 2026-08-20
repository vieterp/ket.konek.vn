/**
 * Bộ định tuyến của ứng dụng.
 *
 * Route nhóm theo **nhóm màn hình** (`src/features/*`), không theo module
 * backend — xem bảng "IA màn hình ≠ ranh giới module" trong
 * docs/design-guidelines.md §3. Đường dẫn viết bằng tiếng Việt không dấu, trùng
 * tên thư mục feature, nên đọc URL là biết đang ở màn hình nào.
 *
 * Mọi route nghiệp vụ nằm **dưới** `SessionGate`: gác một lần ở gốc thay vì
 * mỗi màn hình tự kiểm phiên. Route con chỉ được vẽ khi phiên đã đầy đủ và đã
 * chọn dữ liệu kế toán.
 *
 * `createBrowserRouter` (không phải hash router) vì bundle này còn phải phục vụ
 * được qua HTTP trong chế độ trình duyệt LAN ở v1.x.
 *
 * Ngoại lệ duy nhất nằm ngoài `SessionGate` là `/kitchen-sink` — trang duyệt
 * design system, và **chỉ có khi đang chạy máy chủ dev** (quyết định H8). Nằm
 * ngoài gác phiên là có chủ đích: người thiết kế mở nó khi chưa có server nào
 * chạy và chưa có tài khoản nào.
 *
 * Cổng là `__DEV_TOOLS__` (`command === 'serve'` trong `vite.config.ts`), KHÔNG
 * phải `import.meta.env.DEV`. Cờ `DEV` đọc `NODE_ENV`, nên
 * `NODE_ENV=development pnpm build` từng cho ra một bản giao khách có route này
 * — tức một đường vào ứng dụng không qua `SessionGate`. `__DEV_TOOLS__` là hằng
 * lúc dựng cấu hình và không biến môi trường nào bẻ được; Rollup loại cả nhánh
 * lẫn cây import phía sau khỏi bundle.
 */

import type { ReactElement } from 'react'
import type { RouteObject } from 'react-router-dom'
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom'

import { NAVIGATION } from '@/app/navigation'
import { PlaceholderPage } from '@/app/placeholder-page'
import { SessionGate } from '@/app/session-gate'
import { DataGridBenchPage } from '@/features/bench/data-grid-bench-page'
import { CatalogListPage, OpeningBalancePage, PartnerPage, SettingsPage } from '@/features/danh-muc-thiet-lap'
import { KitchenSinkPage } from '@/features/kitchen-sink/kitchen-sink-page'
import {
  JournalVoucherForm,
  ReportCatalogPage,
  TrialBalancePage,
  VoucherListPage,
} from '@/features/so-sach-thue'

const [home, ...groups] = NAVIGATION

/**
 * Nhóm 07 là nhóm đầu tiên có ruột thật (lát 3D): bốn route thay một trang tạm.
 * Đường dẫn con tiếng Việt không dấu, cùng quy ước với đường dẫn nhóm.
 */
const danhMucThietLapRoutes: RouteObject[] = [
  {
    path: 'danh-muc-thiet-lap',
    children: [
      { index: true, element: <Navigate to="danh-muc/doi-tac" replace /> },
      { path: 'danh-muc/:segment', element: <CatalogListPage /> },
      { path: 'doi-tac/:id', element: <PartnerPage /> },
      { path: 'so-du-ban-dau', element: <OpeningBalancePage /> },
      { path: 'thiet-lap', element: <SettingsPage /> },
    ],
  },
]

/**
 * Nhóm 09 (Sổ sách & Thuế), lát 4E: ba route thay một trang tạm — danh sách
 * chứng từ, form chứng từ nghiệp vụ khác, bảng cân đối tài khoản.
 *
 * `chung-tu/moi` khai TRƯỚC `chung-tu/:id`: hai route cùng cấp, không phải
 * lồng nhau, nên thứ tự khớp của `react-router` mới là thứ quyết định — khai
 * `:id` trước sẽ nuốt mất `/moi` (khớp `id = "moi"`) và không bao giờ tới
 * được nhánh tạo mới.
 */
const soSachThueRoutes: RouteObject[] = [
  {
    path: 'so-sach-thue',
    children: [
      { index: true, element: <Navigate to="chung-tu" replace /> },
      { path: 'chung-tu', element: <VoucherListPage /> },
      { path: 'chung-tu/moi', element: <JournalVoucherForm /> },
      { path: 'chung-tu/:id', element: <JournalVoucherForm /> },
      { path: 'bang-can-doi-tai-khoan', element: <TrialBalancePage /> },
      { path: 'bao-cao', element: <ReportCatalogPage /> },
    ],
  },
]

/** Rỗng trong MỌI bản dựng — xem ghi chú đầu tệp. */
export const devOnlyRoutes: RouteObject[] = __DEV_TOOLS__
  ? [
      { path: '/kitchen-sink', element: <KitchenSinkPage /> },
      // Trang đo lưới nhập liệu (spike S3). Cùng cổng, cùng lý do: nó phơi một
      // kênh đo trên `window` và không đi qua `SessionGate`.
      { path: '/bench/data-grid', element: <DataGridBenchPage /> },
    ]
  : []

const router = createBrowserRouter([
  ...devOnlyRoutes,
  {
    path: '/',
    element: <SessionGate />,
    children: [
      ...(home === undefined ? [] : [{ index: true, element: <PlaceholderPage item={home} /> }]),
      ...groups
        .filter((item) => item.path !== '/danh-muc-thiet-lap' && item.path !== '/so-sach-thue')
        .map((item) => ({
          path: item.path.slice(1),
          element: <PlaceholderPage item={item} />,
        })),
      ...danhMucThietLapRoutes,
      ...soSachThueRoutes,
      // Đường dẫn lạ (người dùng gõ tay, dấu trang cũ sau khi đổi IA) rơi về
      // trang tổng quan thay vì một trang trắng không lối ra.
      ...(home === undefined ? [] : [{ path: '*', element: <PlaceholderPage item={home} /> }]),
    ],
  },
])

export function AppRouter(): ReactElement {
  return <RouterProvider router={router} />
}
