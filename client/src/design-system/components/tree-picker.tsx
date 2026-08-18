/**
 * Cây chọn một nút từ dữ liệu phân cấp, nạp con **khi mở** từng nhánh.
 *
 * Vì sao nạp lười thay vì nhận cả cây: danh mục phân cấp phía server chỉ có hai
 * đường đọc — con trực tiếp của một nút, hoặc cả nhánh của một nút đã biết —
 * và danh mục vật tư có thể tới mười nghìn dòng. Component vì thế nhận
 * `loadChildren` chứ không nhận mảng nút: chỗ gọi quyết định dữ liệu đến từ
 * đâu, cây chỉ lo chuyện mở/đóng/chọn.
 *
 * Là component design system nên **không phụ thuộc `src/lib/`** (xem
 * `components/index.ts`): mọi chữ đi vào bằng prop, dữ liệu đi vào bằng
 * callback. Hai chỗ dùng ở lát 3D: khung trái của màn hình danh mục (chọn nhánh
 * đang xem) và ô "Thuộc nhóm" trong drawer (chọn nút cha, `selectableGroups`).
 *
 * Bàn phím theo khuôn tree view WAI-ARIA: ↑/↓ di chuyển, → mở nhánh (hoặc vào
 * con đầu), ← đóng nhánh (hoặc lên cha), Enter/Space chọn. Focus kiểu roving
 * tabindex — cả cây là **một** trạm Tab, đúng như một ô nhập.
 */

import type { KeyboardEvent, ReactElement } from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

export interface TreePickerNode {
  readonly id: number
  readonly code: string
  readonly label: string
  readonly isGroup: boolean
}

export interface TreePickerProps {
  readonly label: string
  /** `id` của nút đang chọn; `null` = chưa chọn / gốc. */
  readonly selectedId: number | null
  readonly onSelect: (node: TreePickerNode | null) => void
  /** Trả về con trực tiếp của `parentId` (`null` = tầng gốc). */
  readonly loadChildren: (parentId: number | null) => Promise<readonly TreePickerNode[]>
  /** Cho chọn cả nút nhóm (ô "Thuộc nhóm") hay chỉ nút lá (ô tham chiếu). */
  readonly selectableGroups?: boolean
  /** Nhãn dòng gốc, ví dụ "Tất cả" — chọn nó là `onSelect(null)`. */
  readonly rootLabel: string
  readonly loadingLabel: string
  readonly emptyLabel: string
  readonly errorLabel: string
}

interface BranchState {
  readonly status: 'loading' | 'ready' | 'error'
  readonly children: readonly TreePickerNode[]
}

/** Khóa giữ tầng gốc trong map nhánh — id thật luôn dương nên `-1` không đụng ai. */
const ROOT = -1

/** Một dòng đang vẽ — dựng phẳng từ các nhánh đã mở để điều hướng ↑/↓ đơn giản. */
interface VisibleRow {
  readonly node: TreePickerNode | null
  readonly depth: number
  readonly parentId: number | null
}

export function TreePicker({
  label,
  selectedId,
  onSelect,
  loadChildren,
  selectableGroups = false,
  rootLabel,
  loadingLabel,
  emptyLabel,
  errorLabel,
}: TreePickerProps): ReactElement {
  // Khóa `-1` giữ tầng gốc: `Map<number | null>` hợp lệ nhưng đọc khó hơn hẳn.
  // Tầng gốc khai "đang tải" ngay từ state khởi tạo — effect bên dưới chỉ chờ
  // promise và ghi kết quả, không setState đồng bộ trong thân effect.
  const [branches, setBranches] = useState<ReadonlyMap<number, BranchState>>(
    () => new Map([[ROOT, { status: 'loading', children: [] }]]),
  )
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set())
  const [focusedIndex, setFocusedIndex] = useState(0)
  const treeRef = useRef<HTMLUListElement>(null)

  const load = useCallback(
    (key: number): void => {
      setBranches((current) => {
        if (current.has(key)) {
          return current
        }
        const next = new Map(current)
        next.set(key, { status: 'loading', children: [] })
        return next
      })
      loadChildren(key === ROOT ? null : key)
        .then((children) => {
          setBranches((current) => {
            const next = new Map(current)
            next.set(key, { status: 'ready', children })
            return next
          })
        })
        .catch(() => {
          setBranches((current) => {
            const next = new Map(current)
            next.set(key, { status: 'error', children: [] })
            return next
          })
        })
    },
    [loadChildren],
  )

  // Nạp tầng gốc một lần (và nạp lại nếu nguồn dữ liệu đổi). Không đi qua
  // `load` vì khóa ROOT đã có sẵn trong state — `load` sẽ bỏ qua nó.
  useEffect(() => {
    let cancelled = false
    loadChildren(null)
      .then((children) => {
        if (!cancelled) {
          setBranches((current) => new Map(current).set(ROOT, { status: 'ready', children }))
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBranches((current) => new Map(current).set(ROOT, { status: 'error', children: [] }))
        }
      })
    return () => {
      cancelled = true
    }
  }, [loadChildren])

  function toggle(node: TreePickerNode): void {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(node.id)) {
        next.delete(node.id)
      } else {
        next.add(node.id)
        load(node.id)
      }
      return next
    })
  }

  /** Dựng danh sách dòng đang thấy — gốc trước, rồi duyệt trước theo nhánh đã mở. */
  function visibleRows(): VisibleRow[] {
    const rows: VisibleRow[] = [{ node: null, depth: 0, parentId: null }]
    const walk = (parentKey: number, depth: number, parentId: number | null): void => {
      const branch = branches.get(parentKey)
      if (branch?.status !== 'ready') {
        return
      }
      for (const child of branch.children) {
        rows.push({ node: child, depth, parentId })
        if (child.isGroup && expanded.has(child.id)) {
          walk(child.id, depth + 1, child.id)
        }
      }
    }
    walk(ROOT, 1, null)
    return rows
  }

  const rows = visibleRows()
  const rootBranch = branches.get(ROOT)

  function selectRow(row: VisibleRow): void {
    if (row.node === null) {
      onSelect(null)
      return
    }
    if (row.node.isGroup && !selectableGroups) {
      toggle(row.node)
      return
    }
    onSelect(row.node)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLUListElement>): void {
    const row = rows[focusedIndex]
    if (row === undefined) {
      return
    }
    if (event.key === 'ArrowDown') {
      setFocusedIndex((index) => Math.min(index + 1, rows.length - 1))
    } else if (event.key === 'ArrowUp') {
      setFocusedIndex((index) => Math.max(index - 1, 0))
    } else if (event.key === 'ArrowRight' && row.node?.isGroup) {
      if (expanded.has(row.node.id)) {
        setFocusedIndex((index) => Math.min(index + 1, rows.length - 1))
      } else {
        toggle(row.node)
      }
    } else if (event.key === 'ArrowLeft') {
      if (row.node?.isGroup && expanded.has(row.node.id)) {
        toggle(row.node)
      } else {
        const parentIndex = rows.findIndex((candidate) => candidate.node?.id === row.parentId)
        setFocusedIndex(parentIndex >= 0 ? parentIndex : 0)
      }
    } else if (event.key === 'Enter' || event.key === ' ') {
      selectRow(row)
    } else {
      return
    }
    event.preventDefault()
  }

  // Focus đi theo dòng đang trỏ khi điều hướng bằng bàn phím: roving tabindex
  // chỉ hoạt động nếu phần tử mang `tabIndex=0` thật sự nhận focus.
  useEffect(() => {
    const active = treeRef.current?.querySelector<HTMLElement>('[tabindex="0"]')
    if (treeRef.current?.contains(document.activeElement)) {
      active?.focus()
    }
  }, [focusedIndex])

  return (
    <div className="flex flex-col gap-1">
      <p className="text-sm font-medium text-text-default">{label}</p>
      <ul
        ref={treeRef}
        role="tree"
        aria-label={label}
        onKeyDown={handleKeyDown}
        className="max-h-64 overflow-y-auto rounded border border-border-default bg-surface py-1 text-sm"
      >
        {rows.map((row, index) => {
          const node = row.node
          const isSelected = node === null ? selectedId === null : selectedId === node.id
          const isExpanded = node?.isGroup === true ? expanded.has(node.id) : undefined
          return (
            <li
              key={node?.id ?? 'root'}
              role="treeitem"
              aria-selected={isSelected}
              aria-level={row.depth + 1}
              {...(isExpanded === undefined ? {} : { 'aria-expanded': isExpanded })}
              tabIndex={index === focusedIndex ? 0 : -1}
              onClick={() => {
                setFocusedIndex(index)
                selectRow(row)
              }}
              onFocus={() => {
                setFocusedIndex(index)
              }}
              style={{ paddingLeft: `${String(row.depth * 16 + 8)}px` }}
              className={`flex cursor-pointer items-center gap-1 py-1 pr-2 ${
                isSelected
                  ? 'bg-navy-50 font-semibold text-primary'
                  : 'text-text-default hover:bg-primary/10'
              }`}
            >
              {node?.isGroup === true && (
                <button
                  type="button"
                  tabIndex={-1}
                  aria-hidden
                  className="shrink-0 text-text-muted"
                  onClick={(event) => {
                    event.stopPropagation()
                    toggle(node)
                  }}
                >
                  {isExpanded === true ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
              )}
              <span className="truncate">
                {node === null ? rootLabel : `${node.code} — ${node.label}`}
              </span>
            </li>
          )
        })}
        {rootBranch?.status === 'loading' && (
          <li className="px-2 py-1 text-text-muted">{loadingLabel}</li>
        )}
        {rootBranch?.status === 'error' && (
          <li className="px-2 py-1 text-red-600">{errorLabel}</li>
        )}
        {rootBranch?.status === 'ready' && rootBranch.children.length === 0 && (
          <li className="px-2 py-1 text-text-muted">{emptyLabel}</li>
        )}
      </ul>
    </div>
  )
}
