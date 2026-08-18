/**
 * Thẻ công nợ trên màn hình đối tác — nguyên tắc nhóm 07: "thẻ công nợ hiện
 * ngay" cạnh thông tin danh mục.
 *
 * Ở phase 3 thẻ này là **chỗ giữ chỗ có chủ đích**: số dư công nợ đọc
 * `ar_ap_ledger` của module `receivables` (phase 7), và BFF
 * `partners/{id}/overview` hoãn tới đó (H56). Vẽ khung ngay từ bây giờ để bố
 * cục màn hình không phải xếp lại khi số liệu thật xuất hiện — phase 7 chỉ
 * thay ruột thẻ, không đụng trang.
 */

import type { ReactElement } from 'react'
import { Wallet } from 'lucide-react'

import { useI18n } from '@/lib/i18n'

export function PartnerDebtCard(): ReactElement {
  const { t } = useI18n()

  return (
    <section
      aria-label={t('partner.debt.title')}
      className="rounded border border-border-default bg-background p-4"
    >
      <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold text-primary">
        <Wallet size={16} aria-hidden />
        {t('partner.debt.title')}
      </h2>
      <p className="text-sm text-text-muted">{t('partner.debt.pending')}</p>
    </section>
  )
}
