/**
 * Lưới an toàn cuối cùng: một lỗi lúc render phải thành **màn hình có chữ**,
 * không phải một trang trắng.
 *
 * Vì sao cần: React 19 gỡ toàn bộ cây khi một lỗi render không được bắt. Với
 * kiến trúc provider lồng nhau của ứng dụng này, một exception trong provider
 * ngoài cùng (`ThemeProvider`) xóa sạch mọi thứ — người dùng thấy đúng một
 * trang trắng, không mã lỗi, không nút thử lại, không gì để đọc cho bộ phận hỗ
 * trợ. Với phần mềm chạy trong LAN ở phòng kế toán, "gọi hỗ trợ và không mô tả
 * được gì" là kịch bản tệ nhất.
 *
 * Ca đã biết dẫn tới đây: trình duyệt chặn site-data làm `localStorage` ném
 * ngay lúc render (nay đã bọc bằng `lib/safe-storage.ts`, nhưng lưới này bắt cả
 * những nguyên nhân chưa biết).
 *
 * **Chữ ở đây hard-code song ngữ, không đi qua i18n.** Có chủ đích: `I18nProvider`
 * nằm *bên trong* lưới này, nên gọi `useI18n()` ở đây sẽ ném tiếp đúng lúc đang
 * xử lý một lỗi — và biến một lỗi thành trang trắng, đúng thứ cần tránh. Cũng
 * không dùng token màu vì lỗi có thể đến từ chính tầng CSS.
 *
 * Đây là một trong hai chỗ duy nhất của ứng dụng còn dùng class component —
 * React chưa có API hook nào tương đương `componentDidCatch`.
 */

import type { ErrorInfo, ReactElement, ReactNode } from 'react'
import { Component } from 'react'

interface Props {
  readonly children: ReactNode
}

interface State {
  readonly message: string | null
}

export class AppErrorBoundary extends Component<Props, State> {
  public override state: State = { message: null }

  public static getDerivedStateFromError(error: unknown): State {
    return { message: error instanceof Error ? error.message : String(error) }
  }

  public override componentDidCatch(error: unknown, info: ErrorInfo): void {
    // Console là kênh chẩn đoán duy nhất có mặt chắc chắn ở đây: tầng ghi log
    // qua HTTP có thể chính là thứ vừa hỏng.
    console.error('[ket] lỗi render không bắt được', error, info.componentStack)
  }

  public override render(): ReactNode {
    const { message } = this.state
    if (message === null) {
      return this.props.children
    }
    return <ErrorScreen message={message} />
  }
}

function ErrorScreen({ message }: { readonly message: string }): ReactElement {
  return (
    <div
      role="alert"
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
        background: '#f5f7fa',
        color: '#1f2937',
        fontFamily: "'Be Vietnam Pro', system-ui, sans-serif",
      }}
    >
      <div style={{ maxWidth: '32rem', border: '2px solid #1b365d', background: '#fff', padding: '24px' }}>
        <h1 style={{ margin: '0 0 8px', fontSize: '18px', color: '#1b365d' }}>
          Ứng dụng gặp lỗi không mong muốn
        </h1>
        <p style={{ margin: '0 0 4px', fontSize: '14px' }}>
          Tải lại trang để thử lại. Nếu vẫn lỗi, cung cấp dòng chữ dưới đây cho bộ phận hỗ trợ.
        </p>
        <p style={{ margin: '0 0 16px', fontSize: '13px', color: '#6b7280' }}>
          The application hit an unexpected error. Reload to retry; if it persists, send the
          message below to support.
        </p>
        <pre
          style={{
            margin: 0,
            padding: '12px',
            background: '#f5f7fa',
            border: '1px solid #c5d3e8',
            fontSize: '12px',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {message}
        </pre>
      </div>
    </div>
  )
}
