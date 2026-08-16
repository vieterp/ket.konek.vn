import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// Font tự host — sản phẩm phải chạy offline hoàn toàn (LD-01), không nạp font
// qua CDN. Bốn cân nặng theo brand: 400/500/600/700.
// Cần CẢ HAI subset: `vietnamese` cho giao diện tiếng Việt, `latin` cho chuỗi
// tiếng Anh (FR-NFR-034 đa ngôn ngữ vi/en, cột `name_en` trên danh mục) — thiếu
// latin thì chữ tiếng Anh rơi về font dự phòng và lệch khỏi design.
import '@fontsource/be-vietnam-pro/latin-400.css'
import '@fontsource/be-vietnam-pro/latin-500.css'
import '@fontsource/be-vietnam-pro/latin-600.css'
import '@fontsource/be-vietnam-pro/latin-700.css'
import '@fontsource/be-vietnam-pro/vietnamese-400.css'
import '@fontsource/be-vietnam-pro/vietnamese-500.css'
import '@fontsource/be-vietnam-pro/vietnamese-600.css'
import '@fontsource/be-vietnam-pro/vietnamese-700.css'

import { AppProviders } from '@/app/providers'
import { AppRouter } from '@/app/router'
import '@/design-system/tokens.css'
import '@/design-system/base.css'

const container = document.getElementById('root')
if (container === null) {
  throw new Error('Không tìm thấy #root trong index.html')
}

createRoot(container).render(
  <StrictMode>
    <AppProviders>
      <AppRouter />
    </AppProviders>
  </StrictMode>,
)
