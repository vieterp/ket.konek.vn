/**
 * Trang đo hiệu năng lưới nhập liệu — spike S3 (phase 2 bước 17).
 *
 * **Công cụ nội bộ, không phải màn hình sản phẩm.** Gác cùng một cổng với
 * `/kitchen-sink`: hằng lúc dựng `__DEV_TOOLS__` (`command === 'serve'`), nên
 * bundle giao khách không có route này và `check-client-bundle.sh` xác nhận lại
 * bằng cách grep chính bản dựng.
 *
 * Vì sao đo ở đây chứ không đo bằng vitest: jsdom không có bố cục và không vẽ
 * gì. Mọi con số "độ trễ" đo trong nó là thời gian chạy JavaScript, tức là đúng
 * phần rẻ nhất, và nó sẽ luôn đẹp kể cả khi lưới thật giật. Ngưỡng của spike S3
 * nói về **cảm giác gõ**, mà cảm giác gõ là khoảng cách từ lúc bấm phím tới lúc
 * ký tự hiện lên màn hình — chỉ đo được trên trình duyệt thật.
 *
 * Cách đo: bắt `keydown` ở pha capture (trước cả trình xử lý của React) rồi
 * chờ **hai** lượt `requestAnimationFrame`. Lượt thứ nhất chạy trước khi trình
 * duyệt vẽ, lượt thứ hai chạy sau — nên hiệu số bao trọn cả xử lý sự kiện, vẽ
 * lại và trình bày. Không dùng Event Timing API: nó làm tròn tới 8ms và ngưỡng
 * tối thiểu 16ms, quá thô cho một ngưỡng 50ms.
 *
 * Playwright đọc kết quả qua `window.__gridBench`; xem
 * `client/bench/data-grid.bench.spec.ts`.
 */

import type { ReactElement } from 'react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { DataGrid } from '@/design-system/components'
import type { DataGridChange, DataGridColumn, DataGridCommitMode } from '@/design-system/components'

interface BenchLine {
  readonly id: string
  readonly code: string
  readonly name: string
  readonly unit: string
  readonly quantity: string
  readonly unitPrice: string
  readonly taxRate: string
  readonly note: string
  readonly amount: string
}

/** Kênh đọc kết quả cho Playwright. Chỉ tồn tại ở bản dev. */
export interface GridBench {
  keySamples: number[]
  /**
   * Thời gian từ lúc bấm dán tới khung hình đã vẽ xong vùng dán.
   *
   * Đo trong chính `onCommit` chứ không trong một listener `paste` riêng. Bản
   * đầu đo bằng listener riêng, và nó **xanh kể cả khi trình xử lý dán không
   * làm gì cả** — nó đo "trình duyệt giao sự kiện + vẽ một khung hình", không
   * đo cài đặt dán. Một phép đo như thế còn tệ hơn không đo, vì nó được ghi vào
   * báo cáo như bằng chứng cho một ngưỡng mà nó không chạm tới.
   */
  lastPasteMs: number | null
  /** Số ô đã cam kết — phép đối chứng để "nhanh" không thể đến từ "không làm gì". */
  commits: number
  rowCount: number
  reset: () => void
}

declare global {
  var __gridBench: GridBench | undefined
}

const COLUMNS: readonly DataGridColumn<BenchLine>[] = [
  { key: 'code', header: 'Mã hàng', width: 120, value: (row) => row.code },
  { key: 'name', header: 'Tên hàng hóa, dịch vụ', value: (row) => row.name },
  { key: 'unit', header: 'ĐVT', width: 70, value: (row) => row.unit },
  {
    key: 'quantity',
    header: 'Số lượng',
    width: 100,
    align: 'right',
    inputMode: 'decimal',
    value: (row) => row.quantity,
  },
  {
    key: 'unitPrice',
    header: 'Đơn giá',
    width: 130,
    align: 'right',
    inputMode: 'decimal',
    value: (row) => row.unitPrice,
  },
  { key: 'taxRate', header: 'Thuế GTGT', width: 90, align: 'right', value: (row) => row.taxRate },
  { key: 'note', header: 'Diễn giải', value: (row) => row.note },
  // Cột do server tính — lưới không nhân số lượng với đơn giá (H15).
  {
    key: 'amount',
    header: 'Thành tiền',
    width: 130,
    align: 'right',
    readOnly: true,
    value: (row) => row.amount,
  },
]

function makeLine(index: number): BenchLine {
  return {
    id: `line-${index}`,
    code: `VT${String(index).padStart(5, '0')}`,
    name: `Vật tư mẫu số ${index} — hàng hóa chịu thuế`,
    unit: 'Cái',
    quantity: `${(index % 40) + 1}`,
    unitPrice: `${(index % 17) * 12500 + 50000}`,
    taxRate: '10%',
    note: '',
    amount: '0',
  }
}

function emptyLine(index: number): BenchLine {
  return {
    id: `line-${index}`,
    code: '',
    name: '',
    unit: '',
    quantity: '',
    unitPrice: '',
    taxRate: '',
    note: '',
    amount: '0',
  }
}

function afterNextPaint(start: number, assign: (elapsed: number) => void): void {
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      assign(performance.now() - start)
    })
  })
}

/**
 * Đo từ lúc bấm phím / bấm dán tới lúc khung hình kế tiếp đã vẽ xong.
 *
 * Trả về `markPasteApplied` để trang gọi **trong `onCommit`** — chỉ khi lưới
 * thật sự đã giao vùng dán ra thì đồng hồ dán mới dừng.
 */
function useBenchProbe(
  rootRef: React.RefObject<HTMLDivElement | null>,
  rowCount: number,
): (changeCount: number) => void {
  const bench = useRef<GridBench | null>(null)
  const pasteStart = useRef<number | null>(null)

  useEffect(() => {
    const probe: GridBench = {
      keySamples: [],
      lastPasteMs: null,
      commits: 0,
      rowCount,
      reset: () => {
        probe.keySamples = []
        probe.lastPasteMs = null
        probe.commits = 0
        pasteStart.current = null
      },
    }
    bench.current = probe
    globalThis.__gridBench = probe

    const root = rootRef.current
    if (root === null) {
      return
    }

    const onKeyDown = (): void => {
      const start = performance.now()
      afterNextPaint(start, (elapsed) => probe.keySamples.push(elapsed))
    }
    // Chỉ **bấm giờ** ở đây; dừng đồng hồ là việc của `onCommit`.
    const onPaste = (): void => {
      pasteStart.current = performance.now()
    }

    root.addEventListener('keydown', onKeyDown, true)
    root.addEventListener('paste', onPaste, true)
    return () => {
      root.removeEventListener('keydown', onKeyDown, true)
      root.removeEventListener('paste', onPaste, true)
      globalThis.__gridBench = undefined
    }
  }, [rootRef, rowCount])

  return (changeCount: number) => {
    const probe = bench.current
    if (probe === null) {
      return
    }
    probe.commits += changeCount
    const start = pasteStart.current
    if (start === null) {
      return
    }
    pasteStart.current = null
    afterNextPaint(start, (elapsed) => {
      probe.lastPasteMs = elapsed
    })
  }
}

export function DataGridBenchPage(): ReactElement {
  const [params] = useSearchParams()
  const rowCount = Number.parseInt(params.get('rows') ?? '500', 10)
  const commitMode: DataGridCommitMode = params.get('mode') === 'live' ? 'live' : 'on-leave'

  const [lines, setLines] = useState<BenchLine[]>(() =>
    Array.from({ length: rowCount }, (_, index) => makeLine(index)),
  )
  const [commits, setCommits] = useState(0)
  const rootRef = useRef<HTMLDivElement | null>(null)
  const markCommitted = useBenchProbe(rootRef, rowCount)

  const summary = useMemo(
    () => `${lines.length} dòng · chế độ cam kết: ${commitMode} · ${commits} lượt cam kết`,
    [lines.length, commitMode, commits],
  )

  function applyChanges(changes: readonly DataGridChange[]): void {
    markCommitted(changes.length)
    setCommits((count) => count + changes.length)
    setLines((previous) => {
      const next = [...previous]
      for (const change of changes) {
        while (next.length <= change.rowIndex) {
          next.push(emptyLine(next.length))
        }
        const current = next[change.rowIndex]
        if (current !== undefined) {
          next[change.rowIndex] = { ...current, [change.columnKey]: change.value }
        }
      }
      return next
    })
  }

  return (
    <div className="min-h-screen bg-screen p-6">
      <h1 className="text-page text-primary">Đo lưới nhập liệu — spike S3</h1>
      <p className="mt-1 text-app text-text-muted" data-testid="bench-summary">
        {summary}
      </p>
      <p className="mt-1 text-meta text-text-muted">
        Tham số URL: <code>?rows=500</code>, <code>?mode=live</code>. Kết quả đo đọc qua{' '}
        <code>window.__gridBench</code>.
      </p>
      <div ref={rootRef} className="mt-4">
        <DataGrid
          columns={COLUMNS}
          rows={lines}
          rowKey={(row) => row.id}
          caption="Chi tiết chứng từ (dữ liệu đo)"
          cellLabel={(header, rowNumber) => `${header}, dòng ${rowNumber}`}
          commitMode={commitMode}
          rowHeight={30}
          height={540}
          onCommit={applyChanges}
        />
      </div>
    </div>
  )
}
