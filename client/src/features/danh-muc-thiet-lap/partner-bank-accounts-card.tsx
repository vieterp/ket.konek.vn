/**
 * Thẻ tài khoản ngân hàng của một đối tác (FR-SYS-033: nhiều tài khoản).
 *
 * Bảng con thật với khóa ngoại thật (H57) — thêm/sửa/xóa gọi thẳng
 * `…/partners/{id}/bank-accounts`. Form thêm nằm ngay dưới bảng thay vì một
 * drawer nữa: drawer trong drawer là mê cung, còn ở trang chi tiết thì chỗ
 * không thiếu.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { LookupOption } from '@/design-system/components'
import { Alert, Button, LookupInput, TextField } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useLookupOptions } from './use-lookup-options'
import { usePartnerBankAccountMutations, usePartnerBankAccounts } from './use-partner'

export function PartnerBankAccountsCard({
  partnerId,
}: {
  readonly partnerId: number
}): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const accounts = usePartnerBankAccounts(partnerId)
  const { create, update, remove } = usePartnerBankAccountMutations(partnerId)

  // Duyệt đủ cả ngân hàng xếp trong nhóm (H-3) — vừa cho ô chọn, vừa để tra
  // tên trên bảng thay vì rơi về `#id`.
  const bankOptions: readonly LookupOption[] = useLookupOptions('banks').data ?? []

  const [adding, setAdding] = useState(false)
  const [bank, setBank] = useState<LookupOption | null>(null)
  const [accountNumber, setAccountNumber] = useState('')
  const [accountHolder, setAccountHolder] = useState('')
  const [bankBranch, setBankBranch] = useState('')
  const [error, setError] = useState<string | null>(null)
  // Xóa hai bước, cùng khuôn với nút xóa của drawer danh mục: thao tác không
  // hoàn tác được thì cú bấm đầu chỉ đổi nhãn, cú bấm thứ hai mới xóa.
  const [confirmingId, setConfirmingId] = useState<number | null>(null)
  // Một khóa cho một lượt thêm, sinh lúc mở form (RT-12).
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : t('error.transport.unreachable'),
    )
  }

  function submit(): void {
    if (bank === null || accountNumber.trim() === '' || accountHolder.trim() === '') {
      setError(t('partner.bank.required'))
      return
    }
    setError(null)
    create.mutate(
      {
        idempotencyKey,
        body: {
          bank_id: bank.id,
          account_number: accountNumber.trim(),
          account_holder: accountHolder.trim(),
          bank_branch: bankBranch.trim() === '' ? null : bankBranch.trim(),
        },
      },
      {
        onSuccess: () => {
          setAdding(false)
          setBank(null)
          setAccountNumber('')
          setAccountHolder('')
          setBankBranch('')
        },
        onError: fail,
      },
    )
  }

  const rows = accounts.data ?? []

  return (
    <section
      aria-label={t('partner.bank.title')}
      className="rounded border border-border-default bg-background p-4"
    >
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-primary">{t('partner.bank.title')}</h2>
        {!readOnly && !adding && (
          <Button
            variant="secondary"
            onClick={() => {
              setAdding(true)
              setError(null)
              setIdempotencyKey(newIdempotencyKey())
            }}
          >
            {t('partner.bank.add')}
          </Button>
        )}
      </div>

      {error !== null && <Alert tone="error">{error}</Alert>}

      {rows.length === 0 && !accounts.isPending && (
        <p className="text-sm text-text-muted">{t('partner.bank.empty')}</p>
      )}

      {rows.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{t('partner.bank.title')}</caption>
          <thead>
            <tr className="text-xs text-text-muted">
              <th className="py-1 pr-2">{t('catalog.field.bank')}</th>
              <th className="py-1 pr-2">{t('catalog.field.bankAccountNumber')}</th>
              <th className="py-1 pr-2">{t('catalog.field.bankAccountHolder')}</th>
              <th className="py-1 pr-2">{t('partner.bank.branch')}</th>
              <th className="py-1 pr-2">{t('partner.bank.default')}</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {rows.map((account) => {
              const bankOption = bankOptions.find((option) => option.id === account.bank_id)
              return (
                <tr key={account.id} className="border-t border-border-default">
                  <td className="py-1.5 pr-2">
                    {bankOption === undefined
                      ? `#${String(account.bank_id)}`
                      : `${bankOption.code} — ${bankOption.label}`}
                  </td>
                  <td className="py-1.5 pr-2">{account.account_number}</td>
                  <td className="py-1.5 pr-2">{account.account_holder}</td>
                  <td className="py-1.5 pr-2">{account.bank_branch ?? ''}</td>
                  <td className="py-1.5 pr-2">
                    {account.is_default ? (
                      '✓'
                    ) : readOnly ? null : (
                      <button
                        type="button"
                        className="text-xs text-secondary hover:underline"
                        onClick={() => {
                          // Hợp đồng PUT là thay-toàn-bộ: gửi đủ mọi trường
                          // với giá trị đang có, chỉ đổi `is_default`.
                          update.mutate(
                            {
                              accountId: account.id,
                              body: {
                                row_version: account.row_version,
                                bank_id: account.bank_id,
                                account_number: account.account_number,
                                account_holder: account.account_holder,
                                bank_branch: account.bank_branch,
                                is_active: account.is_active,
                                is_default: true,
                              },
                            },
                            { onError: fail },
                          )
                        }}
                      >
                        {t('partner.bank.makeDefault')}
                      </button>
                    )}
                  </td>
                  <td className="py-1.5 text-right">
                    {!readOnly && (
                      <button
                        type="button"
                        className="text-xs text-red-600 hover:underline"
                        onClick={() => {
                          if (confirmingId !== account.id) {
                            setConfirmingId(account.id)
                            return
                          }
                          setConfirmingId(null)
                          remove.mutate(account.id, { onError: fail })
                        }}
                      >
                        {confirmingId === account.id
                          ? t('partner.bank.removeConfirm')
                          : t('partner.bank.remove')}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      {adding && (
        <div className="mt-3 flex flex-col gap-3 rounded border border-border-default p-3">
          <LookupInput
            label={`${t('catalog.field.bank')} *`}
            value={bank}
            onChange={setBank}
            options={bankOptions}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('catalog.lookup.empty')}
          />
          <TextField
            label={`${t('catalog.field.bankAccountNumber')} *`}
            value={accountNumber}
            onChange={(event) => {
              setAccountNumber(event.target.value)
            }}
          />
          <TextField
            label={`${t('catalog.field.bankAccountHolder')} *`}
            value={accountHolder}
            onChange={(event) => {
              setAccountHolder(event.target.value)
            }}
          />
          <TextField
            label={t('partner.bank.branch')}
            value={bankBranch}
            onChange={(event) => {
              setBankBranch(event.target.value)
            }}
          />
          <div className="flex gap-2">
            <Button onClick={submit} disabled={create.isPending}>
              {t('catalog.drawer.save')}
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setAdding(false)
                setError(null)
              }}
            >
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      )}
    </section>
  )
}
