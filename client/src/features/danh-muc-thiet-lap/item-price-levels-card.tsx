/**
 * Thẻ mức giá của một mã hàng (FR-SYS-042, bảng con `item_price_levels`).
 *
 * Mỗi dòng: chiều (mua/bán) · mức 1–20 · ĐVT (trống = đơn vị chính, 7C-1) ·
 * giá · tên thang. Sửa = bấm dòng, form điền sẵn, PUT gửi trọn giá trị mới
 * kèm `row_version`. Số tiền gửi nguyên CHUỖI người dùng gõ — client không
 * làm tròn (LD-03).
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

import { SubTableCard, SubTableFormActions, SubTableRowActions } from './sub-table-card'
import { useItemPriceLevelMutations, useItemPriceLevels, useItemUnitChoices, type ItemPriceLevel } from './use-item-pricing'
import { useSubTableEditor } from './use-sub-table-editor'

const PURCHASE = '0'
const SALE = '1'

interface Draft {
  readonly direction: string
  readonly level: string
  readonly unitId: string
  readonly price: string
  readonly label: string
}

function draftOf(row: ItemPriceLevel | 'new'): Draft {
  if (row === 'new') {
    return { direction: SALE, level: '1', unitId: '', price: '', label: '' }
  }
  return {
    direction: String(row.direction),
    level: String(row.level),
    unitId: row.unit_id === null ? '' : String(row.unit_id),
    price: row.price,
    label: row.label ?? '',
  }
}

export function ItemPriceLevelsCard({
  itemId,
  baseUnitId,
}: {
  readonly itemId: number
  readonly baseUnitId: number | null
}): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const rows = useItemPriceLevels(itemId)
  const { create, update, remove } = useItemPriceLevelMutations(itemId)
  const editor = useSubTableEditor<ItemPriceLevel>()
  const units = useItemUnitChoices(itemId, baseUnitId, t('item.prices.baseUnit'))
  const [draft, setDraft] = useState<Draft>(draftOf('new'))

  const directionLabel = (direction: number): string =>
    direction === 0 ? t('catalog.field.priceDirectionPurchase') : t('catalog.field.priceDirectionSale')

  function open(row: ItemPriceLevel | 'new'): void {
    setDraft(draftOf(row))
    if (row === 'new') {
      editor.openNew()
    } else {
      editor.openEdit(row)
    }
  }

  function submit(): void {
    if (draft.price.trim() === '' || draft.level.trim() === '') {
      editor.setError(t('item.prices.required'))
      return
    }
    editor.setError(null)
    const body = {
      direction: Number.parseInt(draft.direction, 10) as 0 | 1,
      level: Number.parseInt(draft.level, 10),
      unit_id: draft.unitId === '' ? null : Number.parseInt(draft.unitId, 10),
      price: draft.price.trim(),
      label: draft.label.trim() === '' ? null : draft.label.trim(),
    }
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
  const set = (patch: Partial<Draft>): void => {
    setDraft((current) => ({ ...current, ...patch }))
  }

  return (
    <SubTableCard
      title={t('item.prices.title')}
      addLabel={t('item.prices.add')}
      canAdd={!readOnly && editor.editing === null}
      onAdd={() => {
        open('new')
      }}
      error={editor.error}
      empty={list.length === 0 && !rows.isPending ? t('item.prices.empty') : null}
      form={
        editor.editing !== null && (
          <div className="mt-3 flex flex-col gap-3 rounded border border-border-default p-3">
            <SelectField
              label={t('catalog.field.priceDirection')}
              value={draft.direction}
              onChange={(event) => {
                set({ direction: event.target.value })
              }}
              options={[
                { value: PURCHASE, label: t('catalog.field.priceDirectionPurchase') },
                { value: SALE, label: t('catalog.field.priceDirectionSale') },
              ]}
            />
            <TextField
              label={`${t('item.prices.level')} *`}
              inputMode="numeric"
              value={draft.level}
              onChange={(event) => {
                set({ level: event.target.value })
              }}
              hint={t('item.prices.levelHint')}
            />
            <SelectField
              label={t('item.prices.unit')}
              value={draft.unitId}
              onChange={(event) => {
                set({ unitId: event.target.value })
              }}
              options={units.options}
            />
            <TextField
              label={`${t('item.prices.price')} *`}
              inputMode="decimal"
              value={draft.price}
              onChange={(event) => {
                set({ price: event.target.value })
              }}
            />
            <TextField
              label={t('item.prices.label')}
              value={draft.label}
              onChange={(event) => {
                set({ label: event.target.value })
              }}
              hint={t('item.prices.labelHint')}
            />
            <SubTableFormActions onSave={submit} onCancel={editor.close} busy={create.isPending || update.isPending} />
          </div>
        )
      }
    >
      {list.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{t('item.prices.title')}</caption>
          <thead>
            <tr className="text-xs text-text-muted">
              <th className="py-1 pr-2">{t('catalog.field.priceDirection')}</th>
              <th className="py-1 pr-2">{t('item.prices.level')}</th>
              <th className="py-1 pr-2">{t('item.prices.unit')}</th>
              <th className="py-1 pr-2 text-right">{t('item.prices.price')}</th>
              <th className="py-1 pr-2">{t('item.prices.label')}</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {list.map((row) => (
              <tr key={row.id} className="border-t border-border-default">
                <td className="py-1.5 pr-2">{directionLabel(row.direction)}</td>
                <td className="py-1.5 pr-2">{row.level}</td>
                <td className="py-1.5 pr-2">{units.labelOf(row.unit_id)}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.price}</td>
                <td className="py-1.5 pr-2">{row.label ?? ''}</td>
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
