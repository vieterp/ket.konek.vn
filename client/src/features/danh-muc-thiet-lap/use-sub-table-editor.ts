/**
 * Trạng thái sửa tại chỗ của một thẻ bảng con (mức giá, bậc chiết khấu, dòng
 * bảng giá — 7H-2b): form thêm/sửa dưới bảng, xóa hai bước, một khóa
 * idempotency cho một lượt thêm (RT-12), lỗi qua `translateErrorCode`.
 *
 * Tách khỏi từng thẻ vì ba thẻ của lát này lặp đúng bộ state ấy — khác nhau
 * chỉ ở các ô của form, thứ vẫn nằm trong thẻ. Thẻ tài khoản ngân hàng của đối
 * tác (3D) giữ bản riêng vì ra đời trước và không có đường sửa.
 */

import { useState } from 'react'

import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError } from '@/lib/session'

export interface SubTableEditor<TRow extends { readonly id: number }> {
  /** Form đang mở cho việc thêm mới (`'new'`) hay sửa một dòng, hoặc đóng. */
  readonly editing: TRow | 'new' | null
  readonly error: string | null
  readonly confirmingId: number | null
  readonly idempotencyKey: string
  readonly openNew: () => void
  readonly openEdit: (row: TRow) => void
  readonly close: () => void
  readonly setError: (message: string | null) => void
  /** Chuyển lỗi HTTP thành câu tiếng người, dùng làm `onError` của mutation. */
  readonly fail: (caught: unknown) => void
  /** Xóa hai bước: lần đầu trả `false` (đổi nhãn nút), lần hai trả `true`. */
  readonly confirmRemove: (rowId: number) => boolean
}

export function useSubTableEditor<TRow extends { readonly id: number }>(): SubTableEditor<TRow> {
  const { t } = useI18n()
  const [editing, setEditing] = useState<TRow | 'new' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<number | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)

  return {
    editing,
    error,
    confirmingId,
    idempotencyKey,
    openNew: () => {
      setEditing('new')
      setError(null)
      setIdempotencyKey(newIdempotencyKey())
    },
    openEdit: (row) => {
      setEditing(row)
      setError(null)
    },
    close: () => {
      setEditing(null)
      setError(null)
    },
    setError,
    fail: (caught) => {
      setError(
        caught instanceof ApiError
          ? translateErrorCode(t, caught.errorCode)
          : t('error.transport.unreachable'),
      )
      // 409: bản đang sửa đã cũ — đóng form để lượt "Sửa" kế tiếp điền từ
      // dòng vừa nạp lại (hook mutation đã làm mới danh sách).
      if (caught instanceof ApiError && caught.status === 409) {
        setEditing(null)
      }
    },
    confirmRemove: (rowId) => {
      if (confirmingId !== rowId) {
        setConfirmingId(rowId)
        return false
      }
      setConfirmingId(null)
      return true
    },
  }
}
