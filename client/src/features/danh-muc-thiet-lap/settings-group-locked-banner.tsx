/**
 * Banner của nhóm thiết lập "chốt một lần" (U14, FR-SYS-004).
 *
 * Câu chữ nói **hệ quả** ("số liệu ghi sau sẽ khác luật với số liệu đã ghi")
 * chứ không nói cơ chế — người đọc là kế toán viên, không phải người viết
 * phần mềm. Banner đứng cố định trên đầu nhóm, kể cả khi mọi mục còn sửa được:
 * cảnh báo trước khi đổi mới là cảnh báo; sau khi đổi là lời trách.
 */

import type { ReactElement } from 'react'

import { Alert } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

export function SettingsGroupLockedBanner(): ReactElement {
  const { t } = useI18n()

  return <Alert tone="warning">{t('setup.decidedOnce.banner')}</Alert>
}
