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
 */

import type { ReactElement } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'

import { NAVIGATION } from '@/app/navigation'
import { PlaceholderPage } from '@/app/placeholder-page'
import { SessionGate } from '@/app/session-gate'

const [home, ...groups] = NAVIGATION

const router = createBrowserRouter([
  {
    path: '/',
    element: <SessionGate />,
    children: [
      ...(home === undefined ? [] : [{ index: true, element: <PlaceholderPage item={home} /> }]),
      ...groups.map((item) => ({
        path: item.path.slice(1),
        element: <PlaceholderPage item={item} />,
      })),
      // Đường dẫn lạ (người dùng gõ tay, dấu trang cũ sau khi đổi IA) rơi về
      // trang tổng quan thay vì một trang trắng không lối ra.
      ...(home === undefined ? [] : [{ path: '*', element: <PlaceholderPage item={home} /> }]),
    ],
  },
])

export function AppRouter(): ReactElement {
  return <RouterProvider router={router} />
}
