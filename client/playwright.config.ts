/**
 * Cấu hình cho bộ đo hiệu năng lưới nhập liệu (spike S3).
 *
 * Đây **không** phải bộ test e2e của ứng dụng. Nó chỉ có một việc: chạy trang
 * `/bench/data-grid` trên Chromium thật và đọc số đo. Ngưỡng của spike S3 nói
 * về cảm giác gõ, mà cảm giác gõ chỉ đo được ở nơi có bố cục và có vẽ — jsdom
 * không có cả hai.
 *
 * Máy chủ phải là `vite` (dev), không phải `vite preview`: trang đo gác sau
 * `__DEV_TOOLS__` = `command === 'serve'`, nên trong một bản dựng nó không tồn
 * tại. Đó là điều kiện đúng, không phải chỗ vướng — chính `client-bundle-check`
 * khẳng định lại như vậy.
 *
 * Chỉ Chromium: WebKit và Firefox không mở `Input.imeSetComposition` của CDP,
 * mà đó là đường duy nhất dựng được một tổ hợp IME **thật** trong máy tự động.
 */

import { defineConfig, devices } from '@playwright/test'

const PORT = 5173

export default defineConfig({
  testDir: './bench',
  testMatch: '**/*.bench.spec.ts',
  // Đo hiệu năng thì chạy song song là tự phá số đo của mình.
  workers: 1,
  fullyParallel: false,
  reporter: [['list']],
  use: {
    baseURL: `http://localhost:${PORT}`,
    // Cửa sổ cố định để số dòng nhìn thấy không đổi theo máy chạy.
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'pnpm exec vite',
    url: `http://localhost:${PORT}`,
    // Mặc định của Playwright (`!process.env.CI`). Ép `true` vô điều kiện là
    // một cái bẫy trên máy lập trình: một `vite` cũ đang giữ cổng 5173 từ
    // nhánh hay worktree khác sẽ được dùng lại, và bộ đo báo xanh cho MÃ KHÁC.
    timeout: 60_000,
  },
})
