/**
 * Băng lỗi của form chứng từ: câu lỗi chính + (nếu có) toàn bộ vi phạm mà
 * validator ghi sổ trả về trong `posting.invalid`, liệt kê thành danh sách
 * gạch đầu dòng — MỘT băng, không tách nhiều băng cho từng vi phạm.
 *
 * Khi MỌI vi phạm là cảnh báo xác nhận được (FR-SYS-062 mức "Cảnh báo" —
 * `details.warning`), băng đổi giọng `warning` và mang nút "Vẫn ghi sổ?": bấm
 * là gửi lại đúng lệnh vừa bị từ chối kèm `acknowledge_warnings=true`. Nút đặt
 * NGAY TRONG băng chứ không mở hộp thoại riêng — codebase không có modal xác
 * nhận, và hai lần bấm tại chỗ là quy ước sẵn có (nút Xóa).
 */

import type { ReactElement } from 'react'

import { Alert, Button } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { Violation } from './journal-violations'
import { allAcknowledgeableWarnings } from './journal-violations'

export function JournalViolationsAlert({
  error,
  violations,
  onAcknowledge,
  busy = false,
}: {
  readonly error: string
  readonly violations: readonly Violation[]
  /** Có mặt = form biết gửi lại lệnh vừa bị từ chối kèm xác nhận cảnh báo. */
  readonly onAcknowledge?: (() => void) | undefined
  readonly busy?: boolean
}): ReactElement {
  const { t } = useI18n()
  const acknowledgeable = onAcknowledge !== undefined && allAcknowledgeableWarnings(violations)

  return (
    <Alert tone={acknowledgeable ? 'warning' : 'error'}>
      <p>{acknowledgeable ? t('gl.form.warningsTitle') : error}</p>
      {violations.length > 0 && (
        <ul className="mt-1 list-disc pl-5">
          {violations.map((violation, index) => (
            <li key={index}>
              {violation.line_no === undefined
                ? violation.message
                : t('gl.line.violationRow', {
                    row: String(violation.line_no),
                    message: violation.message,
                  })}
            </li>
          ))}
        </ul>
      )}
      {acknowledgeable && (
        <div className="mt-2">
          <Button variant="secondary" disabled={busy} onClick={onAcknowledge}>
            {t('gl.form.acknowledgePost')}
          </Button>
        </div>
      )}
    </Alert>
  )
}
