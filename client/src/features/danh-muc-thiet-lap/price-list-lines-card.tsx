/**
 * Thẻ dòng của một bảng giá (FR-SAL-020, bảng con `price_list_lines`).
 *
 * Mỗi dòng: mã hàng · ĐVT (trống = đơn vị chính của mã đó) · SL từ (theo đơn
 * vị của chính dòng — 7C-1, khác bậc chiết khấu) · giá. Mã hàng tra server
 * (`search=`) vì danh mục vật tư vượt trần trang; tên của dòng đã lưu tra bù
 * theo `ids=`. Ô ĐVT phụ thuộc mã hàng đang chọn trên form.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { LookupOption } from '@/design-system/components'
import { LookupInput, SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

import { SubTableCard, SubTableFormActions, SubTableRowActions } from './sub-table-card'
import { useItemUnitChoices } from './use-item-pricing'
import { useMasterSearchLookup } from './use-master-search-lookup'
import { usePriceListLineMutations, usePriceListLines, type PriceListLine } from './use-price-list-lines'
import { useSubTableEditor } from './use-sub-table-editor'

interface Draft {
  readonly item: LookupOption | null
  readonly unitId: string
  readonly minQuantity: string
  readonly price: string
}

const EMPTY_DRAFT: Draft = { item: null, unitId: '', minQuantity: '1', price: '' }

export function PriceListLinesCard({ priceListId }: { readonly priceListId: number }): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const rows = usePriceListLines(priceListId)
  const { create, update, remove } = usePriceListLineMutations(priceListId)
  const editor = useSubTableEditor<PriceListLine>()
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)

  const list = rows.data ?? []
  const itemLookup = useMasterSearchLookup('items', list.map((row) => row.item_id))
  const unitLookup = useMasterSearchLookup('units_of_measure', list.flatMap((row) => (row.unit_id === null ? [] : [row.unit_id])))
  // ĐVT của mã hàng ĐANG CHỌN trên form — đơn vị chính không cần tra tên ở
  // đây (nhãn "Đơn vị chính" là đủ cho ô chọn của một mã đã biết).
  const draftUnits = useItemUnitChoices(draft.item?.id ?? null, null, t('item.prices.baseUnit'))

  const itemLabel = (itemId: number): string => {
    const option = itemLookup.byId.get(itemId)
    return option === undefined ? `#${String(itemId)}` : `${option.code} — ${option.label}`
  }
  const unitLabel = (unitId: number | null): string => {
    if (unitId === null) {
      return t('item.prices.baseUnit')
    }
    const option = unitLookup.byId.get(unitId)
    return option === undefined ? `#${String(unitId)}` : `${option.code} — ${option.label}`
  }

  function open(row: PriceListLine | 'new'): void {
    if (row === 'new') {
      setDraft(EMPTY_DRAFT)
      editor.openNew()
      return
    }
    setDraft({
      item: itemLookup.byId.get(row.item_id) ?? { id: row.item_id, code: `#${String(row.item_id)}`, label: '' },
      unitId: row.unit_id === null ? '' : String(row.unit_id),
      minQuantity: row.min_quantity,
      price: row.price,
    })
    editor.openEdit(row)
  }

  function submit(): void {
    if (draft.item === null || draft.price.trim() === '' || draft.minQuantity.trim() === '') {
      editor.setError(t('priceList.lines.required'))
      return
    }
    editor.setError(null)
    const body = {
      item_id: draft.item.id,
      unit_id: draft.unitId === '' ? null : Number.parseInt(draft.unitId, 10),
      min_quantity: draft.minQuantity.trim(),
      price: draft.price.trim(),
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

  const set = (patch: Partial<Draft>): void => {
    setDraft((current) => ({ ...current, ...patch }))
  }

  return (
    <SubTableCard
      title={t('priceList.lines.title')}
      addLabel={t('priceList.lines.add')}
      canAdd={!readOnly && editor.editing === null}
      onAdd={() => {
        open('new')
      }}
      error={editor.error}
      empty={list.length === 0 && !rows.isPending ? t('priceList.lines.empty') : null}
      form={
        editor.editing !== null && (
          <div className="mt-3 flex flex-col gap-3 rounded border border-border-default p-3">
            <LookupInput
              label={`${t('priceList.lines.item')} *`}
              value={draft.item}
              onChange={(option) => {
                // Đổi mã hàng thì ĐVT quy đổi của mã cũ không còn nghĩa — về đơn vị chính.
                set({ item: option, unitId: '' })
              }}
              options={itemLookup.options}
              onQueryChange={itemLookup.searchFor}
              clearLabel={t('catalog.lookup.clear')}
              emptyLabel={t('catalog.lookup.empty')}
            />
            <SelectField
              label={t('item.prices.unit')}
              value={draft.unitId}
              onChange={(event) => {
                set({ unitId: event.target.value })
              }}
              options={draftUnits.options}
              disabled={draft.item === null}
            />
            <TextField
              label={`${t('priceList.lines.minQuantity')} *`}
              inputMode="decimal"
              value={draft.minQuantity}
              onChange={(event) => {
                set({ minQuantity: event.target.value })
              }}
              hint={t('priceList.lines.minQuantityHint')}
            />
            <TextField
              label={`${t('item.prices.price')} *`}
              inputMode="decimal"
              value={draft.price}
              onChange={(event) => {
                set({ price: event.target.value })
              }}
            />
            <SubTableFormActions onSave={submit} onCancel={editor.close} busy={create.isPending || update.isPending} />
          </div>
        )
      }
    >
      {list.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{t('priceList.lines.title')}</caption>
          <thead>
            <tr className="text-xs text-text-muted">
              <th className="py-1 pr-2">{t('priceList.lines.item')}</th>
              <th className="py-1 pr-2">{t('item.prices.unit')}</th>
              <th className="py-1 pr-2 text-right">{t('priceList.lines.minQuantity')}</th>
              <th className="py-1 pr-2 text-right">{t('item.prices.price')}</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {list.map((row) => (
              <tr key={row.id} className="border-t border-border-default">
                <td className="py-1.5 pr-2">{itemLabel(row.item_id)}</td>
                <td className="py-1.5 pr-2">{unitLabel(row.unit_id)}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.min_quantity}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.price}</td>
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
