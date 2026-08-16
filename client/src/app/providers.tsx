/**
 * Bọc ứng dụng bằng ba lớp ngữ cảnh, theo đúng thứ tự phụ thuộc.
 *
 * `I18n` ngoài cùng vì màn hình lỗi của hai lớp trong cũng cần chữ. `Session`
 * ở giữa vì nó dựng `ApiClient` mà `QueryClient` sẽ gọi qua. `QueryClient`
 * trong cùng.
 *
 * `QueryClient` dựng trong `useState` chứ không phải hằng ở tầng module: một
 * hằng dùng chung sẽ mang bộ nhớ đệm của lần chạy trước sang bài test kế tiếp,
 * và đó là loại xanh-sai khó lần nhất.
 */

import type { ReactElement, ReactNode } from 'react'
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { I18nProvider } from '@/lib/i18n'
import { ApiError, SessionProvider } from '@/lib/session'

function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Bản cài chạy trong LAN với dữ liệu do chính người trong phòng nhập.
        // Tự tải lại khi chuyển cửa sổ chỉ tạo tiếng ồn; màn hình nào cần số
        // mới nhất (hàng đợi tác vụ, đối chiếu) sẽ tự khai `refetchInterval`.
        refetchOnWindowFocus: false,
        // Thử lại một lần **và chỉ khi** lỗi không phải câu trả lời dứt khoát
        // của máy chủ: mạng LAN chớp thì lần hai thường ăn, còn `403` hay `404`
        // thì lần hai cũng thế — thử lại chỉ bắt người dùng nhìn màn hình chờ
        // lâu gấp đôi trước khi thấy đúng thông báo đang chờ.
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
            return false
          }
          return failureCount < 1
        },
      },
    },
  })
}

export function AppProviders({ children }: { children: ReactNode }): ReactElement {
  const [queryClient] = useState(createQueryClient)

  return (
    <I18nProvider>
      <SessionProvider>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </SessionProvider>
    </I18nProvider>
  )
}
