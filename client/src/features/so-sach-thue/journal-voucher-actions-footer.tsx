/**
 * Hàng nút cuối form chứng từ: Ghi sổ/Bỏ ghi sổ theo trạng thái, Xóa (hai lần
 * bấm để xác nhận — quy ước sẵn có), Hủy, Cất.
 *
 * Chỉ chứng từ ĐÃ LƯU (`voucher !== null`) mới có Ghi sổ/Bỏ ghi sổ/Xóa; phiên
 * chỉ-đọc ẩn mọi nút ghi. Component không tự gọi mutation — nhận callback từ
 * `VoucherFormBody`, nơi giữ state `busy`/lỗi dùng chung với phần lưới.
 */

import type { ReactElement } from 'react'

import { Button } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import { VOUCHER_STATUS_DRAFT, VOUCHER_STATUS_POSTED } from './voucher-status'

export interface JournalVoucherActionsFooterProps {
  readonly voucherStatus: number | null
  readonly readOnly: boolean
  readonly busy: boolean
  readonly confirmDelete: boolean
  readonly onPost: () => void
  readonly onUnpost: () => void
  readonly onDelete: () => void
  readonly onCancel: () => void
  readonly onSave: () => void
}

export function JournalVoucherActionsFooter({
  voucherStatus,
  readOnly,
  busy,
  confirmDelete,
  onPost,
  onUnpost,
  onDelete,
  onCancel,
  onSave,
}: JournalVoucherActionsFooterProps): ReactElement {
  const { t } = useI18n()
  const isExisting = voucherStatus !== null

  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex gap-2">
        {isExisting && !readOnly && voucherStatus === VOUCHER_STATUS_DRAFT && (
          <Button variant="secondary" disabled={busy} onClick={onPost}>
            {t('gl.form.post')}
          </Button>
        )}
        {isExisting && !readOnly && voucherStatus === VOUCHER_STATUS_POSTED && (
          <Button variant="secondary" disabled={busy} onClick={onUnpost}>
            {t('gl.form.unpost')}
          </Button>
        )}
        {isExisting && !readOnly && voucherStatus === VOUCHER_STATUS_DRAFT && (
          <Button variant="ghost" disabled={busy} onClick={onDelete}>
            {confirmDelete ? t('gl.form.deleteConfirm') : t('gl.form.delete')}
          </Button>
        )}
      </div>
      <div className="flex gap-2">
        <Button variant="secondary" disabled={busy} onClick={onCancel}>
          {t('common.cancel')}
        </Button>
        {!readOnly && (
          <Button disabled={busy} onClick={onSave}>
            {busy ? t('gl.form.saving') : t('gl.form.save')}
          </Button>
        )}
      </div>
    </div>
  )
}
