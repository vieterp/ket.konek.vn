import type { ReactElement } from 'react'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'

import { AppLayout } from '@/app/layout'

/**
 * Bộ định tuyến của ứng dụng.
 *
 * Route được nhóm theo **nhóm màn hình** (thư mục `src/features/*`), không theo
 * module backend — xem bảng "IA màn hình ≠ ranh giới module" trong
 * docs/system-architecture.md. Phase 1 chỉ có một route gốc.
 *
 * Dùng `createBrowserRouter` (không phải hash router) vì bundle này còn phải
 * phục vụ được qua HTTP trong chế độ trình duyệt LAN ở v1.x.
 */
const router = createBrowserRouter([
  {
    path: '/',
    element: (
      <AppLayout>
        <p className="text-text-muted">
          Khung ứng dụng phase 1 — chưa có chức năng nghiệp vụ.
        </p>
      </AppLayout>
    ),
  },
])

export function AppRouter(): ReactElement {
  return <RouterProvider router={router} />
}
