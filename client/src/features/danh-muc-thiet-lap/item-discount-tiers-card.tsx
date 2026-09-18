/**
 * Thẻ bậc chiết khấu theo số lượng của một mã hàng (FR-SYS-045, bảng con
 * `item_discount_tiers`). Ngưỡng `min_quantity` tính theo ĐƠN VỊ CHÍNH của mã
 * hàng (7C-1: thang so được giữa các dòng ghi bằng đơn vị khác nhau) — hint
 * dưới ô nói rõ điều ấy.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

import { SubTableCard, SubTableFormActions, SubTableRowActions } from './sub-table-card'
import { useItemDiscountTierMutations, useItemDiscountTiers, type ItemDiscountTier } from './use-item-pricing'
import { useSubTableEditor } from './use-sub-table-editor'

interface Draft {
  readonly minQuantity: string
  readonly discountPercent: string
}

function draftOf(row: ItemDiscountTier | 'new'): Draft {
  return row === 'new'
    ? { minQuantity: '', discountPercent: '' }
    : { minQuantity: row.min_quantity, discountPercent: row.discount_percent }
}

export function ItemDiscountTiersCard({ itemId }: { readonly itemId: number }): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const rows = useItemDiscountTiers(itemId)
  const { create, update, remove } = useItemDiscountTierMutations(itemId)
  const editor = useSubTableEditor<ItemDiscountTier>()
  const [draft, setDraft] = useState<Draft>(draftOf('new'))

  function open(row: ItemDiscountTier | 'new'): void {
    setDraft(draftOf(row))
    if (row === 'new') {
      editor.openNew()
    } else {
      editor.openEdit(row)
    }
  }

  function submit(): void {
    if (draft.minQuantity.trim() === '' || draft.discountPercent.trim() === '') {
      editor.setError(t('item.tiers.required'))
      return
    }
    editor.setError(null)
    const body = { min_quantity: draft.minQuantity.trim(), discount_percent: draft.discountPercent.trim() }
    if (editor.editing === 'new') {
      create.mutate({ body, idempotencyKey: editor.idempotencyKey }, { onSuccess: editor.close, onError: editor.fail })
    } else if (editor.editing !== null) {
      update.mutate(
        { rowId: editor.editing.id, body: { ...body, row_version: editor.editing.row_version } },
        { onSuccess: editor.close, onError: editor.fail },
      )
    }
  }

  const list = rows.data ?? []

  return (
    <SubTableCard
      title={t('item.tiers.title')}
      addLabel={t('item.tiers.add')}
      canAdd={!readOnly && editor.editing === null}
      onAdd={() => {
        open('new')
      }}
      error={editor.error}
      empty={list.length === 0 && !rows.isPending ? t('item.tiers.empty') : null}
      form={
        editor.editing !== null && (
          <div className="mt-3 flex flex-col gap-3 rounded border border-border-default p-3">
            <TextField
              label={`${t('item.tiers.minQuantity')} *`}
              inputMode="decimal"
              value={draft.minQuantity}
              onChange={(event) => {
                setDraft((current) => ({ ...current, minQuantity: event.target.value }))
              }}
              hint={t('item.tiers.minQuantityHint')}
            />
            <TextField
              label={`${t('item.tiers.discountPercent')} *`}
              inputMode="decimal"
              value={draft.discountPercent}
              onChange={(event) => {
                setDraft((current) => ({ ...current, discountPercent: event.target.value }))
              }}
            />
            <SubTableFormActions onSave={submit} onCancel={editor.close} busy={create.isPending || update.isPending} />
          </div>
        )
      }
    >
      {list.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{t('item.tiers.title')}</caption>
          <thead>
            <tr className="text-xs text-text-muted">
              <th className="py-1 pr-2 text-right">{t('item.tiers.minQuantity')}</th>
              <th className="py-1 pr-2 text-right">{t('item.tiers.discountPercent')}</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {list.map((row) => (
              <tr key={row.id} className="border-t border-border-default">
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.min_quantity}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.discount_percent}</td>
                {readOnly ? (
                  <td />
                ) : (
                  <SubTableRowActions
                    onEdit={() => {
                      open(row)
                    }}
                    onRemove={() => {
                      if (editor.confirmRemove(row.id)) {
                        remove.mutate(row.id, { onError: editor.fail })
                      }
                    }}
                    confirming={editor.confirmingId === row.id}
                  />
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </SubTableCard>
  )
}
