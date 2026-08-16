/**
 * Hằng do bộ dựng thay thế lúc biên dịch (`vite.config.ts` → `define`).
 *
 * Khai ở đây để `tsc` biết chúng tồn tại mà không cần `any` ở chỗ dùng: mã
 * trong `src/` **không** được `import` `package.json` (xem ghi chú ở
 * `vite.config.ts`), nên phiên bản phải đi vào bundle theo đường này.
 */

/** Phiên bản phát hành, lấy từ `client/package.json` lúc dựng. */
declare const __APP_VERSION__: string
