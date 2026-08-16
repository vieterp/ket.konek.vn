/**
 * Panel trượt từ mép phải — xem/sửa một dòng mà không rời khỏi danh sách.
 *
 * Bốn quyết định, tất cả đều xuất phát từ việc người dùng ở đây đang **nhập
 * liệu**, không phải đang đọc:
 *
 * 1. **Bấm ra nền KHÔNG đóng.** Mọi thư viện dialog đều đóng khi bấm ra ngoài,
 *    và với một hộp thoại xác nhận thì đúng. Với một chứng từ đang gõ dở thì
 *    một cú bấm trượt tay xóa sạch việc vừa làm mà không hỏi câu nào. Chỉ Esc
 *    và nút đóng mới đóng — hai thao tác đều là chủ ý.
 * 2. **Esc thì đóng**, vì đó là phản xạ của người dùng bàn phím và chặn nó lại
 *    làm người ta thấy bị nhốt. Chỗ gọi nào có dữ liệu chưa lưu thì tự hỏi lại
 *    trong `onClose` — đó là việc của màn hình, không phải của cái vỏ.
 * 3. **Bẫy focus.** Không bẫy thì phím Tab đi tiếp xuống sidebar và bảng phía
 *    sau: người dùng bàn phím gõ vào những ô mình không nhìn thấy.
 * 4. **Trả focus về chỗ cũ khi đóng.** Đóng panel mà focus rơi về `<body>` thì
 *    lần Tab kế tiếp bắt đầu lại từ đầu trang — với người vừa mở panel từ dòng
 *    thứ 40 của bảng, đó là mất chỗ hoàn toàn.
 *
 * Mở panel thì focus vào **ô đầu tiên trong phần thân**, không phải nút đóng
 * (nút đóng đứng trước trong DOM nên đó mới là thứ "phần tử focus được đầu
 * tiên" trả về). Người dùng mở panel để **gõ**; bắt họ bấm Tab một nhịp trước
 * mỗi lần sửa một dòng là thuế nhân với vài trăm lần mỗi ngày. Trình đọc màn
 * hình vẫn đọc tên hộp thoại vì focus đi *vào trong* hộp thoại, và nút đóng
 * vẫn tới được bằng Shift+Tab.
 *
 * Vẽ qua `createPortal` vào `<body>`: panel phải nằm trên sidebar và topbar mà
 * không phụ thuộc vào `z-index` hay `overflow` của cái cây đang bọc nó.
 */

import type { KeyboardEvent, ReactElement, ReactNode } from 'react'
import { useEffect, useId, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Phần tử khớp `FOCUSABLE` chưa chắc **focus được thật**: một `<input>` nằm
 * trong khối `display:none` hay `hidden` vẫn khớp selector nhưng không bao giờ
 * nhận được focus.
 *
 * Bỏ qua chuyện này thì cái bẫy focus thủng: nếu phần tử ẩn là phần tử CUỐI
 * danh sách, `last` trỏ vào một thứ `document.activeElement` không bao giờ bằng
 * → nhánh vòng lại không chạy → không `preventDefault` → phím Tab đi thẳng ra
 * ngoài panel, xuống sidebar và bảng phía sau. Đây là ca thường gặp chứ không
 * hiếm: form chứng từ ẩn/hiện ô theo loại chứng từ, khối "Mở rộng" thu gọn.
 *
 * Kiểm bằng cách **đi ngược lên cây** và soi style tính toán, chứ KHÔNG dùng
 * `offsetParent`/`getClientRects()`. Hai cách sau là phép kiểm bố cục: chúng
 * đúng trong trình duyệt thật nhưng vô nghĩa trong jsdom, nơi không có engine
 * bố cục nào — `offsetParent` luôn `null` cho mọi phần tử, kể cả phần tử đang
 * hiện. Dùng chúng thì cái bẫy focus sẽ *không kiểm được bằng test*, mà đây lại
 * đúng là thứ chỉ có test mới giữ được.
 *
 * Đi ngược lên còn bắt được thêm `visibility: hidden` và thuộc tính `hidden`,
 * hai cách ẩn phần tử mà `offsetParent` bỏ sót.
 */
function isReallyFocusable(element: HTMLElement): boolean {
  for (let node: HTMLElement | null = element; node !== null; node = node.parentElement) {
    if (node.hidden) {
      return false
    }
    const style = getComputedStyle(node)
    if (style.display === 'none' || style.visibility === 'hidden') {
      return false
    }
  }
  return true
}

function focusableWithin(root: HTMLElement | null): HTMLElement[] {
  return Array.from(root?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter(isReallyFocusable)
}

export interface DrawerProps {
  readonly open: boolean
  readonly title: string
  readonly onClose: () => void
  /** Nhãn nút đóng cho trình đọc màn hình, ví dụ "Đóng". */
  readonly closeLabel: string
  readonly children: ReactNode
  /** Hàng nút ở đáy panel (Lưu, Hủy…). */
  readonly footer?: ReactNode
}

export function Drawer({
  open,
  title,
  onClose,
  closeLabel,
  children,
  footer,
}: DrawerProps): ReactElement | null {
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) {
      return
    }
    // Ghi nhớ TRƯỚC khi chuyển focus vào panel, nếu không thì thứ ghi nhớ được
    // lại chính là cái nút đóng bên trong panel.
    returnFocusRef.current = document.activeElement as HTMLElement | null
    // Ô đầu tiên trong THÂN panel; panel không có ô nào thì focus chính panel
    // (nó có `tabIndex={-1}` cho đúng trường hợp này).
    const [firstInBody] = focusableWithin(bodyRef.current)
    ;(firstInBody ?? panelRef.current)?.focus()

    return () => {
      // `isConnected` vì phần tử đã mở panel có thể không còn trong DOM: dòng
      // bảng bị lọc đi sau khi lưu, danh sách vừa tải lại. `focus()` trên phần
      // tử mồ côi im lặng không làm gì, focus rơi về `<body>`, và lần Tab kế
      // tiếp bắt đầu lại từ đầu trang — đúng thứ việc trả focus sinh ra để
      // tránh. Khi đó lùi về chính panel gần nhất còn sống là `document.body`
      // thì vô nghĩa, nên ta không làm gì thêm và để trình duyệt giữ nguyên.
      const target = returnFocusRef.current
      if (target?.isConnected === true) {
        target.focus()
      }
    }
  }, [open])

  if (!open) {
    return null
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape') {
      event.stopPropagation()
      onClose()
      return
    }
    if (event.key !== 'Tab') {
      return
    }
    const focusable = focusableWithin(panelRef.current)
    if (focusable.length === 0) {
      event.preventDefault()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (first === undefined || last === undefined) {
      return
    }
    // Vòng lại ở hai đầu — đây chính là cái bẫy. Trường hợp `panelRef` là khi
    // thân panel không có ô nào focus được: khi ấy focus đang nằm trên chính
    // panel, và Shift+Tab từ đó sẽ rơi ra ngoài trang nếu không chặn.
    const onFirst =
      document.activeElement === first || document.activeElement === panelRef.current
    if (event.shiftKey && onFirst) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* Nền mờ chỉ để làm nổi panel; KHÔNG bắt sự kiện bấm (quyết định 1). */}
      <div className="absolute inset-0 bg-navy-900/30" aria-hidden />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="relative flex h-full w-full max-w-xl flex-col border-l-2 border-primary bg-background shadow-xl outline-none"
      >
        <header className="flex items-center justify-between gap-4 border-b border-border-default px-6 py-4">
          <h2 id={titleId} className="text-base font-semibold text-primary">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={closeLabel}
            className="rounded p-1 text-text-muted hover:bg-primary/10 hover:text-primary"
          >
            <X size={18} aria-hidden />
          </button>
        </header>
        <div ref={bodyRef} className="flex-1 overflow-y-auto px-6 py-4">
          {children}
        </div>
        {footer !== undefined && (
          <footer className="flex justify-end gap-2 border-t border-border-default px-6 py-4">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  )
}
