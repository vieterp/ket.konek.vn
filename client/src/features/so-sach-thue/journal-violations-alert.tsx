/**
 * Băng lỗi của form chứng từ: câu lỗi chính + (nếu có) toàn bộ vi phạm mà
 * validator ghi sổ trả về trong `posting.invalid`, liệt kê thành danh sách
 * gạch đầu dòng — MỘT băng, không tách nhiều băng cho từng vi phạm.
 */

import type { ReactElement } from 'react'

import { Alert } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { Violation } from './journal-violations'

export function JournalViolationsAlert({
  error,
  violations,
}: {
  readonly error: string
  readonly violations: readonly Violation[]
}): ReactElement {
  const { t } = useI18n()

  return (
    <Alert tone="error">
      <p>{error}</p>
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
    </Alert>
  )
}
