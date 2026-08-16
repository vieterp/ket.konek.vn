/**
 * Nạp một lần trước mọi tệp test (khai ở `vite.config.ts` → `test.setupFiles`).
 *
 * Hai việc, cả hai đều thuộc loại "quên thì test xanh sai":
 *
 * 1. `@testing-library/jest-dom` — thêm các phép khẳng định về DOM
 *    (`toBeInTheDocument`, `toBeDisabled`). Thiếu nó thì `expect(...).toBeX()`
 *    ném `TypeError` chứ không đỏ ở chỗ đang kiểm.
 * 2. Dọn `localStorage` giữa các test. Phiên đăng nhập lưu ở đó (quyết định
 *    H5), nên một test để lại token sẽ làm test sau bắt đầu ở trạng thái "đã
 *    đăng nhập" — và nó sẽ xanh vì lý do sai.
 */

import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
  localStorage.clear()
})
