import js from '@eslint/js'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist', 'src-tauri/target'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommendedTypeChecked],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      // Luật phụ thuộc #6 (docs/system-architecture.md): web UI chỉ biết REST +
      // type sinh từ OpenAPI. Luồng nghiệp vụ KHÔNG được gọi API Tauri, nếu
      // không thì chế độ "trình duyệt trong LAN" ở v1.x sẽ chết. Cầu nối Tauri
      // (updater, in ấn, chọn tệp, giữ phiên) chỉ được đặt trong `src/lib/tauri/`.
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['@tauri-apps/*'],
              message:
                'Cấm gọi API Tauri ngoài src/lib/tauri/ — giữ đường lên chế độ trình duyệt LAN (luật phụ thuộc #6).',
            },
          ],
        },
      ],
    },
  },
  {
    // Cầu nối Tauri là nơi DUY NHẤT được import @tauri-apps/*.
    files: ['src/lib/tauri/**/*.{ts,tsx}'],
    rules: { 'no-restricted-imports': 'off' },
  },
  {
    // File cấu hình chạy ở Node, không phải trình duyệt.
    files: ['*.config.{js,ts}', 'eslint.config.js'],
    languageOptions: { globals: globals.node },
    extends: [tseslint.configs.disableTypeChecked],
  },
)
