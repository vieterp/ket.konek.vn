/**
 * Tailwind 4 tách plugin PostCSS ra gói riêng `@tailwindcss/postcss`.
 *
 * `autoprefixer` đã bỏ khỏi chuỗi build: Tailwind 4 tự thêm vendor prefix, giữ
 * lại chỉ là một bước thừa.
 */
export default {
  plugins: {
    '@tailwindcss/postcss': {},
  },
}
