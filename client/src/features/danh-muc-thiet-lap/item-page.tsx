/**
 * Màn hình chi tiết mã hàng — nơi khai giá (7H-2b): thẻ thông tin, thẻ mức
 * giá (FR-SYS-042), thẻ bậc chiết khấu (FR-SYS-045). Khuôn trang đối tác:
 * nút "Sửa" mở lại đúng `CatalogEditDrawer` của danh mục vật tư — một form
 * cho một việc, dù đi vào từ danh sách hay từ trang chi tiết.
 *
 * Nút nhóm không có thẻ giá: server 404 có chủ đích cho bảng con của nhóm
 * (`items_common.load_item`) — trang nói thẳng thay vì để hai thẻ đỏ.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'

import { Alert, Button, StatusPill } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { CatalogEditDrawer } from './catalog-edit-drawer'
import { catalogBySlug } from './catalog-registry'
import { extraValue } from './catalog-types'
import { FeatureNav } from './feature-nav'
import { ItemBomCard } from './item-bom-card'
import { ItemDiscountTiersCard } from './item-discount-tiers-card'
import { ItemPriceLevelsCard } from './item-price-levels-card'
import { useCatalogRecord } from './use-catalog'
import { useMasterSearchLookup } from './use-master-search-lookup'

const ITEMS_DEF = catalogBySlug('items')

function InfoRow({ label, value }: { readonly label: string; readonly value: string }): ReactElement | null {
  if (value === '') {
    return null
  }
  return (
    <div className="flex gap-2 text-sm">
      <dt className="w-44 shrink-0 text-text-muted">{label}</dt>
      <dd className="text-text-default">{value}</dd>
    </div>
  )
}

function numberOrNull(value: string | number | boolean | null): number | null {
  return typeof value === 'number' ? value : null
}

export function ItemPage(): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const { id } = useParams()
  const itemId = Number.parseInt(id ?? '', 10)
  const idInvalid = Number.isNaN(itemId)
  const query = useCatalogRecord('items', idInvalid ? null : itemId)
  const [editing, setEditing] = useState(false)

  const item = query.data
  const baseUnitId = item === undefined ? null : numberOrNull(extraValue(item, 'base_unit_id'))
  const warehouseId = item === undefined ? null : numberOrNull(extraValue(item, 'warehouse_id'))
  const unitLookup = useMasterSearchLookup('units_of_measure', baseUnitId === null ? [] : [baseUnitId])
  const warehouseLookup = useMasterSearchLookup('warehouses', warehouseId === null ? [] : [warehouseId])

  const error = idInvalid
    ? translateErrorCode(t, 'master_data.not_found')
    : query.error instanceof ApiError
      ? translateErrorCode(t, query.error.errorCode)
      : query.isError
        ? t('error.transport.unreachable')
        : null

  function lookupLabel(lookup: ReturnType<typeof useMasterSearchLookup>, lookupId: number | null): string {
    if (lookupId === null) {
      return ''
    }
    const option = lookup.byId.get(lookupId)
    return option === undefined ? `#${String(lookupId)}` : `${option.code} — ${option.label}`
  }

  const natureField = ITEMS_DEF?.extraFields.find((field) => field.key === 'nature')
  const natureValue = item === undefined ? null : extraValue(item, 'nature')
  const natureOption = natureField?.options?.find((option) => option.value === natureValue)
  // Định mức chỉ có nghĩa với thứ qua kho (server từ chối dịch vụ) — ẩn thẻ thay vì hiện lỗi.
  const stocked = natureValue === 'goods' || natureValue === 'finished_goods'
  const taxInclusive = item === undefined ? null : extraValue(item, 'price_is_tax_inclusive')

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-4">
        <header className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-3">
            <Link
              to="/danh-muc-thiet-lap/danh-muc/vat-tu-hang-hoa"
              className="flex items-center gap-1 text-sm text-secondary hover:underline"
            >
              <ArrowLeft size={14} aria-hidden />
              {t('item.back')}
            </Link>
            {item !== undefined && (
              <h1 className="text-lg font-semibold text-primary">
                {item.code} — {item.name}
              </h1>
            )}
            {item !== undefined && !item.is_active && (
              <StatusPill tone="todo">{t('catalog.status.inactive')}</StatusPill>
            )}
          </div>
          {item !== undefined && !readOnly && (
            <Button
              variant="secondary"
              onClick={() => {
                setEditing(true)
              }}
            >
              {t('item.edit')}
            </Button>
          )}
        </header>

        {!idInvalid && query.isPending && <p className="text-sm text-text-muted">{t('common.loading')}</p>}
        {error !== null && <Alert tone="error">{error}</Alert>}

        {item !== undefined && (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <section aria-label={t('item.info.title')} className="rounded border border-border-default bg-background p-4">
              <h2 className="mb-2 text-sm font-semibold text-primary">{t('item.info.title')}</h2>
              <dl className="flex flex-col gap-1.5">
                <InfoRow label={t('catalog.field.nature')} value={natureOption === undefined ? '' : t(natureOption.labelKey)} />
                <InfoRow label={t('catalog.field.baseUnit')} value={lookupLabel(unitLookup, baseUnitId)} />
                <InfoRow label={t('catalog.field.defaultWarehouse')} value={lookupLabel(warehouseLookup, warehouseId)} />
                <InfoRow label={t('catalog.field.description')} value={String(extraValue(item, 'description') ?? '')} />
                <InfoRow
                  label={t('catalog.field.priceIsTaxInclusive')}
                  value={
                    taxInclusive === true
                      ? t('catalog.field.priceIsTaxInclusiveYes')
                      : taxInclusive === false
                        ? t('catalog.field.priceIsTaxInclusiveNo')
                        : t('item.info.taxInclusiveBySystem')
                  }
                />
              </dl>
            </section>

            <div className="flex flex-col gap-4">
              {item.is_group ? (
                <Alert tone="info">{t('item.groupHasNoPrices')}</Alert>
              ) : (
                <>
                  <ItemPriceLevelsCard itemId={item.id} baseUnitId={baseUnitId} />
                  <ItemDiscountTiersCard itemId={item.id} />
                  {stocked && <ItemBomCard itemId={item.id} />}
                </>
              )}
            </div>
          </div>
        )}

        {ITEMS_DEF !== undefined && item !== undefined && (
          <CatalogEditDrawer
            catalog={ITEMS_DEF}
            record={item}
            open={editing}
            onClose={() => {
              setEditing(false)
            }}
            onSaved={() => {
              setEditing(false)
            }}
          />
        )}
      </section>
    </div>
  )
}
