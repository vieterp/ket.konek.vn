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
export default defineConfig(({ command }) => ({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
    // Trang duyệt design system `/kitchen-sink` chỉ tồn tại khi đang chạy MÁY
    // CHỦ DEV (`vite`), không bao giờ trong một bản dựng (`vite build`).
    //
    // Trước đây cổng này là `import.meta.env.DEV`, và đó là một cổng thủng:
    // `NODE_ENV=development pnpm exec vite build` cho ra bundle CÓ route
    // `/kitchen-sink` — một đường vào ứng dụng không qua `SessionGate` — kèm cả
    // sourcemap toàn bộ mã nguồn client. `tauri build` gọi `pnpm build` mà
    // không ghim `NODE_ENV`, nên chỉ cần một runner CI hay một máy lập trình có
    // biến đó trong profile là bản giao khách nhiễm.
    //
    // `command === 'serve'` là hằng lúc dựng cấu hình và **không đọc
    // `NODE_ENV`**, nên không có biến môi trường nào bẻ được nó. Rollup thấy
    // `false` và loại cả nhánh lẫn cây import phía sau.
    __DEV_TOOLS__: JSON.stringify(command === 'serve'),
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
    // **Không bao giờ** kèm sourcemap trong một bản dựng: sourcemap mang theo
    // `sourcesContent`, tức là TOÀN BỘ mã nguồn client, và bản dựng là thứ đi
    // vào installer giao cho khách.
    //
    // Trước đây điều kiện là `process.env.NODE_ENV !== 'production'` — treo
    // trên biến môi trường, đúng cùng một lỗi với cổng `/kitchen-sink` cũ.
    // `tauri build` gọi `pnpm build` mà không ghim `NODE_ENV`, nên chỉ cần một
    // runner CI hay một máy lập trình có `NODE_ENV=development` trong profile
    // là installer nhiễm — đo được: 125 tệp nguồn, gồm cả `session.tsx`.
    //
    // Máy chủ dev không dùng tùy chọn này (nó có sourcemap riêng), nên tắt ở
    // đây không làm mất gì khi phát triển.
    sourcemap: false,
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
}))
