/**
 * Cuộn ảo: chỉ vẽ những dòng đang nhìn thấy.
 *
 * Đây là nửa đầu của lời đáp cho spike S3. Một chứng từ 500 dòng × 8 cột là
 * 4.000 ô; vẽ hết ra DOM thì lần dựng đầu tiên đã thấy khựng, mà mỗi lần React
 * đối chiếu lại cây cũng phải đi qua từng ấy node. Với cửa sổ ~30 dòng thì con
 * số 500 (hay 5.000) không còn nằm trong đường tính toán mỗi phím gõ nữa —
 * chi phí chỉ phụ thuộc chiều cao vùng cuộn.
 *
 * Nửa sau nằm ở `data-grid.tsx`: đúng MỘT `<input>` tồn tại tại một thời điểm
 * (H11), và nó không controlled trong lúc gõ (H12).
 *
 * Tự viết ~50 dòng thay vì thêm `@tanstack/react-virtual`: bài toán ở đây là
 * dòng **cao bằng nhau**, tức là hai phép chia. Thư viện kia giải bài toán khó
 * hơn nhiều (chiều cao đo được lúc chạy, cuộn ngang, dính đầu bảng) mà lưới này
 * không dùng tới. Điều kiện đảo lại đã ghi trong báo cáo spike: nếu số đo trượt
 * ngưỡng thì thay bằng thư viện, không cố sửa chỗ này.
 */

import type { RefObject } from 'react'
import { useCallback, useRef, useState } from 'react'

export interface RowWindow {
  /** Dòng đầu tiên được vẽ. */
  readonly startIndex: number
  /** Dòng cuối cùng được vẽ, **không** tính vào (nửa mở). */
  readonly endIndex: number
  /** Chiều cao khối đệm trên, thay cho các dòng chưa vẽ. */
  readonly topPad: number
  readonly bottomPad: number
}

export interface UseRowWindowOptions {
  readonly rowCount: number
  readonly rowHeight: number
  readonly height: number
  /**
   * Số dòng vẽ thừa ngoài mép trên và mép dưới. Không có nó thì cuộn nhanh sẽ
   * thấy một dải trắng ở mép trong lúc chờ lượt vẽ kế tiếp.
   */
  readonly overscan?: number
}

export interface UseRowWindow {
  readonly scrollRef: RefObject<HTMLDivElement | null>
  readonly rowWindow: RowWindow
  readonly onScroll: () => void
  /** Kéo dòng vào tầm nhìn — dùng khi bàn phím đưa ô chọn ra ngoài cửa sổ. */
  readonly scrollRowIntoView: (index: number) => void
}

export function useRowWindow({
  rowCount,
  rowHeight,
  height,
  overscan = 6,
}: UseRowWindowOptions): UseRowWindow {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  // Sự kiện `scroll` bắn dày hơn nhịp vẽ của màn hình. Gộp về một lượt mỗi
  // khung hình để cuộn không tự nó trở thành nguồn giật.
  const framePending = useRef(false)

  const onScroll = useCallback(() => {
    if (framePending.current) {
      return
    }
    framePending.current = true
    requestAnimationFrame(() => {
      framePending.current = false
      setScrollTop(scrollRef.current?.scrollTop ?? 0)
    })
  }, [])

  // Bớt dòng đi (xóa dòng, lọc lại) trong lúc đang cuộn ở đáy sẽ để `scrollTop`
  // trỏ ra ngoài vùng còn lại — cửa sổ rỗng, lưới trắng trơn dù còn dữ liệu.
  // Kẹp lại ngay lúc tính chứ không sửa state trong một effect: trình duyệt tự
  // hạ `scrollTop` của phần tử khi nội dung ngắn đi, nên chỉ còn phép tính ở
  // đây là lệch — và một phép tính lệch thì sửa bằng phép tính.
  const maxScrollTop = Math.max(0, rowCount * rowHeight - height)
  const clampedScrollTop = Math.min(scrollTop, maxScrollTop)

  const visibleCount = Math.ceil(height / rowHeight)
  const startIndex = Math.max(0, Math.floor(clampedScrollTop / rowHeight) - overscan)
  const endIndex = Math.min(rowCount, startIndex + visibleCount + overscan * 2)

  const scrollRowIntoView = useCallback(
    (index: number) => {
      const element = scrollRef.current
      if (element === null) {
        return
      }
      const top = index * rowHeight
      const bottom = top + rowHeight
      // Đặt thẳng `scrollTop` chứ không gọi `scrollIntoView()`: hàm kia cuộn cả
      // trang khi vùng cuộn của lưới nằm trong một trang dài hơn, và nó không
      // làm gì trong jsdom nên cái bẫy này sẽ không kiểm được bằng test.
      if (top < element.scrollTop) {
        element.scrollTop = top
      } else if (bottom > element.scrollTop + height) {
        element.scrollTop = bottom - height
      } else {
        return
      }
      setScrollTop(element.scrollTop)
    },
    [rowHeight, height],
  )

  return {
    scrollRef,
    rowWindow: {
      startIndex,
      endIndex,
      topPad: startIndex * rowHeight,
      bottomPad: Math.max(0, (rowCount - endIndex) * rowHeight),
    },
    onScroll,
    scrollRowIntoView,
  }
}
