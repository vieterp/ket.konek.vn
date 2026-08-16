import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Phiên bản đọc thẳng từ `package.json` — cùng con số mà
// `.github/scripts/check_version_consistency.py` canh ở năm tệp khai phiên bản.
// Client gửi nó trong header `X-Client-Version` mỗi request ghi; server từ chối
// `426` nếu cũ hơn `min_client_version` (bước 19, LD-05).
//
// Đọc lúc dựng cấu hình chứ không `import` JSON vào mã trong `src/`: `src/` chỉ
// được chứa mã chạy trên trình duyệt, và một `import` như thế kéo cả tệp
// `package.json` (gồm danh sách phụ thuộc) vào bundle giao khách.
const packageJson = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf-8'),
) as { version: string }

// Bundle web này phải mở được bằng trình duyệt thường, không chỉ trong Tauri
// (topology "trình duyệt trong LAN" ở v1.x — cấm thiết kế thứ gì chặn nó).
// Vì vậy: không dùng plugin nào gắn cứng vào Tauri, không nội xạ API Tauri.
export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@api-types': fileURLToPath(new URL('./packages/api-types', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    // Tauri v2 trên Windows/macOS đều theo được ES2022.
    target: 'es2022',
    // Sourcemap chỉ ở bản không phải production — bản giao khách không kèm
    // sourcemap để không phát tán mã nguồn client theo installer.
    sourcemap: process.env.NODE_ENV !== 'production',
  },
  test: {
    // `jsdom` chứ không `happy-dom`: màn hình đăng nhập dựa vào form thật
    // (submit bằng Enter, `required`, focus), và jsdom là bản mô phỏng sát
    // trình duyệt hơn ở đúng những chỗ đó.
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
