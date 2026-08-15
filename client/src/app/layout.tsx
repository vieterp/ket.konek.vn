import type { ReactElement, ReactNode } from 'react'

/**
 * Khung trang: sidebar + topbar theo design "Konek Screens 2a".
 *
 * Phase 1 chỉ dựng vỏ để chứng minh tokens và bundle chạy. Điều hướng thật,
 * sidebar theo nhóm màn hình, và các nguyên tắc UX U1–U14 làm từ phase 2 trở đi
 * (xem docs/design-guidelines.md).
 */
export function AppLayout({ children }: { children: ReactNode }): ReactElement {
  return (
    <div className="min-h-screen bg-screen text-text-default">
      <header className="border-b-2 border-navy-700 bg-surface px-6 py-4">
        <h1 className="text-h3 text-navy-700">Konek Kế toán</h1>
      </header>
      <main className="p-6">{children}</main>
    </div>
  )
}
