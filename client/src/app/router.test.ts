/**
 * Trang duyệt design system chỉ được tồn tại khi đang chạy máy chủ dev.
 *
 * Vì sao đáng một bài test riêng: `/kitchen-sink` nằm **ngoài** `SessionGate`,
 * nên nếu nó lọt vào bản giao khách thì đó là một đường vào ứng dụng **không
 * qua đăng nhập**. Trang đó không đọc dữ liệu thật nên không lộ số liệu, nhưng
 * một route bỏ qua cổng phiên là thứ không được có mặt trong sản phẩm — và
 * "quên gác" là loại lỗi không ai nhìn thấy khi review diff.
 *
 * **Giới hạn của chính bài test này:** nó ép `__DEV_TOOLS__` rồi khẳng định cái
 * ternary, nên nó chứng minh *mã* phản ứng đúng với cờ — KHÔNG chứng minh được
 * *bản dựng thật* đặt cờ đúng. Bất biến thật do `make client-bundle-check` canh:
 * dựng rồi grep chính bundle. Hai phép kiểm bổ sung nhau; đừng bỏ cái nào.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

async function loadRoutes(devTools: boolean): Promise<{ path?: string }[]> {
  vi.stubGlobal('__DEV_TOOLS__', devTools)
  vi.resetModules()
  const module = await import('./router')
  return module.devOnlyRoutes
}

describe('devOnlyRoutes', () => {
  it('có /kitchen-sink khi chạy máy chủ dev', async () => {
    const routes = await loadRoutes(true)

    expect(routes.map((route) => route.path)).toEqual(['/kitchen-sink'])
  })

  it('RỖNG trong bản dựng', async () => {
    const routes = await loadRoutes(false)

    expect(routes).toHaveLength(0)
  })
})
