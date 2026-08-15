import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Bundle web này phải mở được bằng trình duyệt thường, không chỉ trong Tauri
// (topology "trình duyệt trong LAN" ở v1.x — cấm thiết kế thứ gì chặn nó).
// Vì vậy: không dùng plugin nào gắn cứng vào Tauri, không nội xạ API Tauri.
export default defineConfig({
  plugins: [react()],
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
})
