/**
 * Cây chọn nút — kiểm bằng tương tác thật: nạp lười, chọn, và bàn phím.
 *
 * `loadChildren` là hàm giả trả dữ liệu tĩnh; không có fetch nào ở đây vì
 * component design system không được biết tới API (xem `components/index.ts`).
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { TreePickerNode } from './tree-picker'
import { TreePicker } from './tree-picker'

const TREE: Record<string, readonly TreePickerNode[]> = {
  root: [
    { id: 1, code: 'NHOM1', label: 'Nhóm một', isGroup: true },
    { id: 2, code: 'LA2', label: 'Lá hai', isGroup: false },
  ],
  '1': [{ id: 3, code: 'LA3', label: 'Lá ba trong nhóm', isGroup: false }],
}

function loadChildren(parentId: number | null): Promise<readonly TreePickerNode[]> {
  return Promise.resolve(TREE[parentId === null ? 'root' : String(parentId)] ?? [])
}

function renderPicker(
  onSelect: (node: TreePickerNode | null) => void,
  selectableGroups = false,
): void {
  render(
    <TreePicker
      label="Nhóm danh mục"
      selectedId={null}
      onSelect={onSelect}
      loadChildren={loadChildren}
      selectableGroups={selectableGroups}
      rootLabel="Tất cả"
      loadingLabel="Đang tải…"
      emptyLabel="Trống"
      errorLabel="Lỗi tải"
    />,
  )
}

describe('TreePicker', () => {
  it('nạp tầng gốc và hiện các nút', async () => {
    renderPicker(vi.fn())
    expect(await screen.findByText('NHOM1 — Nhóm một')).toBeInTheDocument()
    expect(screen.getByText('LA2 — Lá hai')).toBeInTheDocument()
  })

  it('bấm nút lá thì chọn; bấm nhóm (khi không cho chọn nhóm) thì mở nhánh', async () => {
    const onSelect = vi.fn()
    renderPicker(onSelect)
    const user = userEvent.setup()

    await user.click(await screen.findByText('LA2 — Lá hai'))
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ id: 2, code: 'LA2' }),
    )

    await user.click(screen.getByText('NHOM1 — Nhóm một'))
    // Nhóm không chọn được → không thêm lượt gọi, nhưng con của nó hiện ra.
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('LA3 — Lá ba trong nhóm')).toBeInTheDocument()
  })

  it('cho chọn nhóm khi `selectableGroups` (ô "Thuộc nhóm")', async () => {
    const onSelect = vi.fn()
    renderPicker(onSelect, true)
    const user = userEvent.setup()

    await user.click(await screen.findByText('NHOM1 — Nhóm một'))
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
  })

  it('chọn dòng gốc trả về null', async () => {
    const onSelect = vi.fn()
    renderPicker(onSelect)
    const user = userEvent.setup()

    await screen.findByText('NHOM1 — Nhóm một')
    await user.click(screen.getByText('Tất cả'))
    expect(onSelect).toHaveBeenCalledWith(null)
  })

  it('điều hướng bằng bàn phím: ↓ rồi Enter chọn nút lá', async () => {
    const onSelect = vi.fn()
    renderPicker(onSelect)
    const user = userEvent.setup()

    await screen.findByText('LA2 — Lá hai')
    const tree = screen.getByRole('tree')
    const rootRow = screen.getByText('Tất cả').closest('[role="treeitem"]')
    expect(rootRow).not.toBeNull()
    ;(rootRow as HTMLElement).focus()

    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 2 }))
    expect(tree).toBeInTheDocument()
  })

  it('mũi tên phải mở nhánh nhóm', async () => {
    renderPicker(vi.fn())
    const user = userEvent.setup()

    await screen.findByText('NHOM1 — Nhóm một')
    const rootRow = screen.getByText('Tất cả').closest('[role="treeitem"]') as HTMLElement
    rootRow.focus()

    await user.keyboard('{ArrowDown}{ArrowRight}')
    expect(await screen.findByText('LA3 — Lá ba trong nhóm')).toBeInTheDocument()
  })

  it('nguồn dữ liệu hỏng thì hiện nhãn lỗi, không trắng khung', async () => {
    render(
      <TreePicker
        label="Nhóm danh mục"
        selectedId={null}
        onSelect={vi.fn()}
        loadChildren={() => Promise.reject(new Error('mất mạng'))}
        rootLabel="Tất cả"
        loadingLabel="Đang tải…"
        emptyLabel="Trống"
        errorLabel="Lỗi tải"
      />,
    )
    await waitFor(() => {
      expect(screen.getByText('Lỗi tải')).toBeInTheDocument()
    })
  })
})
