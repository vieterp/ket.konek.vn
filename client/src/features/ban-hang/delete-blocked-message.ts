/**
 * Câu tiếng Việt cho lượt xóa chứng từ bán bị vướng tờ hóa đơn điện tử NHÁP
 * (nợ 7D). Server để khóa ngoại `fk_einvoices_source_voucher` (`RESTRICT`) giữ
 * chứng từ lại — có chủ đích, không `CASCADE` — và middleware chỉ nói được
 * "vi phạm ràng buộc" kèm tên ràng buộc trong `details.constraint`. Client là
 * nơi có đủ ngữ cảnh để nói ra BƯỚC PHẢI LÀM: hủy tờ nháp ở nhóm 02 trước.
 *
 * Hàm thuần: nhận `problem` của `ApiError`, trả câu + liên kết, hoặc `null`
 * khi lỗi là thứ khác (câu chung của `translateErrorCode` xử tiếp).
 */

import type { ProblemDetails } from '@api-types'

import type { Translate } from '@/lib/i18n'

/** Tên ràng buộc ở `modules/einvoice/models.py` — đổi bên ấy thì đổi đây. */
export const EINVOICE_SOURCE_CONSTRAINT = 'fk_einvoices_source_voucher'

export interface DeleteBlockedMessage {
  readonly text: string
  readonly linkLabel: string
  readonly href: string
}

export function deleteBlockedByDraftEInvoice(
  t: Translate,
  problem: ProblemDetails,
  voucherId: string,
): DeleteBlockedMessage | null {
  if (problem.details?.['constraint'] !== EINVOICE_SOURCE_CONSTRAINT) {
    return null
  }
  return {
    text: t('sales.form.deleteBlockedByDraftEInvoice'),
    linkLabel: t('sales.form.deleteBlockedLink'),
    href: `/hoa-don-dien-tu?source_voucher_id=${voucherId}`,
  }
}
