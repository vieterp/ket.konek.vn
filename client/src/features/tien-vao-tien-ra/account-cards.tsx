/**
 * Hàng thẻ tài khoản của màn "Tiền vào tiền ra" (U-Quỹ, design nhóm 03): Quỹ
 * tiền mặt + từng TK ngân hàng, mỗi thẻ hiện số dư; bấm thẻ đổi ngữ cảnh lưới
 * bên dưới — không tải lại trang.
 *
 * Thẻ ngân hàng hiện CẢ nguyên tệ lẫn số quy đổi khi hai con số khác nhau
 * (FR-BNK-041 đòi hai trục). `unassigned_deposit` hiện thành một dòng ghi chú
 * riêng — phần số dư TK 112 không quy được về TK ngân hàng nào (bút toán GLE
 * gõ thẳng + chứng từ quỹ chạm 112, ghi chú M-3 review 6D): tổng thẻ + số này
 * = tổng TK 112 trên bảng cân đối, người dùng nhìn ra ngay vì sao hai nơi lệch.
 */

import type { ReactElement } from 'react'

import { formatMoney } from '@/lib/formatters'
import { useI18n } from '@/lib/i18n'

import type { BankAccountCard, CashAccountCard, SelectedCard } from './use-cashflow'

function cardKey(card: SelectedCard): string {
  return card.source === 'cash' ? `cash:${card.accountCode}` : `bank:${String(card.bankAccountId)}`
}

const CARD_CLASS = (active: boolean): string =>
  `min-w-[180px] shrink-0 rounded border px-3 py-2 text-left ${
    active
      ? 'border-ocean-500 bg-navy-50 ring-2 ring-ocean-200'
      : 'border-border-default bg-background hover:border-ocean-500'
  }`

export function AccountCards({
  cashAccounts,
  bankAccounts,
  unassignedDeposit,
  selected,
  onSelect,
}: {
  readonly cashAccounts: readonly CashAccountCard[]
  readonly bankAccounts: readonly BankAccountCard[]
  readonly unassignedDeposit: string
  readonly selected: SelectedCard | null
  readonly onSelect: (card: SelectedCard) => void
}): ReactElement {
  const { t, locale } = useI18n()
  const selectedKey = selected === null ? null : cardKey(selected)

  if (cashAccounts.length === 0 && bankAccounts.length === 0) {
    return <p className="text-app text-text-muted">{t('cashflow.cards.empty')}</p>
  }

  return (
    <div className="flex flex-col gap-1">
      <div
        role="listbox"
        aria-label={t('cashflow.cards.label')}
        className="flex gap-2 overflow-x-auto pb-1"
      >
        {cashAccounts.map((card) => {
          const value: SelectedCard = { source: 'cash', accountCode: card.account_code }
          const active = selectedKey === cardKey(value)
          return (
            <button
              key={cardKey(value)}
              type="button"
              role="option"
              aria-selected={active}
              className={CARD_CLASS(active)}
              onClick={() => {
                onSelect(value)
              }}
            >
              <span className="block text-xs text-text-muted">
                {t('cashflow.cards.cashTag')} · {card.account_code}
              </span>
              <span className="block truncate text-sm font-medium text-text-default">
                {card.account_name}
              </span>
              <span className="block text-right text-sm font-semibold text-primary">
                {formatMoney(card.balance, locale)}
              </span>
            </button>
          )
        })}
        {bankAccounts.map((card) => {
          const value: SelectedCard = { source: 'bank', bankAccountId: card.bank_account_id }
          const active = selectedKey === cardKey(value)
          return (
            <button
              key={cardKey(value)}
              type="button"
              role="option"
              aria-selected={active}
              className={CARD_CLASS(active)}
              onClick={() => {
                onSelect(value)
              }}
            >
              <span className="block truncate text-xs text-text-muted">
                {card.bank_name ?? t('cashflow.cards.bankTag')} · {card.bank_account_code}
              </span>
              <span className="block truncate text-sm font-medium text-text-default">
                {card.bank_account_name}
              </span>
              <span className="block text-right text-sm font-semibold text-primary">
                {formatMoney(card.balance, locale)}
              </span>
              {/* Số nguyên tệ chỉ đáng một dòng khi nó KHÁC số quy đổi. */}
              {card.balance_fc !== card.balance && (
                <span className="block text-right text-xs text-text-muted">
                  {formatMoney(card.balance_fc, locale)} {card.currency_code}
                </span>
              )}
            </button>
          )
        })}
      </div>
      {unassignedDeposit !== '0' && unassignedDeposit !== '0.00' && (
        <p className="text-xs text-text-muted">
          {t('cashflow.cards.unassignedDeposit', {
            amount: formatMoney(unassignedDeposit, locale),
          })}
        </p>
      )}
    </div>
  )
}
