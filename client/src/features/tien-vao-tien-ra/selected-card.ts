/**
 * Luật chọn thẻ ngữ cảnh của màn "Tiền vào tiền ra" — tách thành hàm thuần để
 * test thẳng được (review 6F-2, M-6: bản sửa L-4 hạ cánh không có cổng canh).
 *
 * Ba vế, theo thứ tự:
 * 1. Thẻ người dùng ĐÃ bấm, nếu nó còn trong overview mới — TK ngân hàng vừa
 *    bị gộp/ngừng theo dõi mà vẫn giữ làm ngữ cảnh là lưới truy vấn một thẻ ma
 *    (nợ L-4 review 6F-1).
 * 2. Overview ĐANG tải thì giữ lựa chọn — đừng nhảy thẻ giữa chừng.
 * 3. Chưa bấm gì (hoặc thẻ đã biến mất) thì thẻ đầu tiên, quỹ đứng trước.
 */

import type { CashflowOverview, SelectedCard } from './use-cashflow'

export function resolveSelectedCard(
  chosen: SelectedCard | null,
  overview: CashflowOverview | undefined,
): SelectedCard | null {
  const chosenStillExists =
    chosen === null
      ? false
      : overview === undefined
        ? true
        : chosen.source === 'cash'
          ? overview.cash_accounts.some((card) => card.account_code === chosen.accountCode)
          : overview.bank_accounts.some((card) => card.bank_account_id === chosen.bankAccountId)
  if (chosenStillExists) {
    return chosen
  }
  const firstCash = overview?.cash_accounts[0]
  if (firstCash !== undefined) {
    return { source: 'cash', accountCode: firstCash.account_code }
  }
  const firstBank = overview?.bank_accounts[0]
  if (firstBank !== undefined) {
    return { source: 'bank', bankAccountId: firstBank.bank_account_id }
  }
  return null
}
