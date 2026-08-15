/**
 * Tailwind config — mở rộng từ brand asset
 * `uploads/brand-assets/tokens/tailwind.config.js` v1.0 (Claude Design project
 * 91a2c87a-3304-418f-a296-dc584cd56cfe). Thang màu navy/ocean/sky và fontSize
 * chép nguyên văn từ brand asset; KHÔNG chỉnh mã màu ở đây.
 *
 * Phần thêm cho ứng dụng (không có trong brand asset):
 *   - `screen`, `surface`, `border-default`, `text-default`… trỏ về CSS variable
 *     trong `src/design-system/tokens.css` để dark mode đổi theo biến, không
 *     phải viết lại lớp tiện ích.
 *   - `darkMode: ['class', '[data-theme="dark"]']` cho phép người dùng chọn tay.
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  darkMode: ['class', '[data-theme="dark"]'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Primary - Navy
        navy: {
          50: '#E8EDF4',
          100: '#C5D3E8',
          200: '#A2B9DC',
          300: '#5B7BA3',
          400: '#3D5F89',
          500: '#2D4A6F',
          600: '#234058',
          700: '#1B365D',
          800: '#152B4A',
          900: '#0F1F38',
        },
        // Secondary - Ocean Blue
        ocean: {
          50: '#EBF4FC',
          100: '#C9E0F5',
          200: '#A7CCEE',
          300: '#85B8E7',
          400: '#63A4E0',
          500: '#4A90D9',
          600: '#3A7AC4',
          700: '#2E6DB5',
          800: '#235A96',
          900: '#1A4777',
        },
        // Accent - Sky Blue
        sky: {
          50: '#F0F7FC',
          100: '#D9EBF7',
          200: '#B3D7EF',
          300: '#8DC3E7',
          400: '#7CB9E8',
          500: '#5BA5DE',
          600: '#4891D4',
          700: '#357DCA',
        },
        // Semantic — đọc từ CSS variable nên tự đổi theo dark mode.
        screen: 'var(--color-screen)',
        surface: 'var(--color-surface)',
        'text-default': 'var(--color-text)',
        'text-muted': 'var(--color-text-muted)',
        'border-default': 'var(--color-border)',
      },
      fontFamily: {
        sans: ['Be Vietnam Pro', 'sans-serif'],
        primary: ['Be Vietnam Pro', 'sans-serif'],
      },
      fontSize: {
        hero: ['3.5rem', { lineHeight: '1.2', fontWeight: '700' }],
        h1: ['2.5rem', { lineHeight: '1.2', fontWeight: '700' }],
        h2: ['2rem', { lineHeight: '1.3', fontWeight: '600' }],
        h3: ['1.5rem', { lineHeight: '1.4', fontWeight: '600' }],
      },
    },
  },
  plugins: [],
}
