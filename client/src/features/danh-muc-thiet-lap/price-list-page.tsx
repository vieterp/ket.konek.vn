/**
 * Màn hình một bảng giá (7H-2b): đầu trang là hồ sơ bảng giá (chiều, hiệu
 * lực, đối tác/hợp đồng áp), thân là thẻ dòng giá. Nút "Sửa" mở lại
 * `CatalogEditDrawer` của danh mục bảng giá — cùng khuôn trang đối tác/mã hàng.
 *
 * Nút nhóm không có dòng giá (server 404 có chủ đích, `load_price_list`): trang
 * nói thẳng thay vì để thẻ đỏ.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'

import { Alert, Button, StatusPill } from '@/design-system/components'
import { formatDate } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { CatalogEditDrawer } from './catalog-edit-drawer'
import { catalogBySlug } from './catalog-registry'
import { extraValue } from './catalog-types'
import { FeatureNav } from './feature-nav'
import { PriceListLinesCard } from './price-list-lines-card'
import { useCatalogRecord } from './use-catalog'
import { useMasterSearchLookup } from './use-master-search-lookup'

const PRICE_LISTS_DEF = catalogBySlug('price_lists')

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

export function PriceListPage(): ReactElement {
  const { t, locale } = useI18n()
  const { readOnly } = useSession()
  const { id } = useParams()
  const priceListId = Number.parseInt(id ?? '', 10)
  const idInvalid = Number.isNaN(priceListId)
  const query = useCatalogRecord('price_lists', idInvalid ? null : priceListId)
  const [editing, setEditing] = useState(false)

  const priceList = query.data
  const partnerId = priceList === undefined ? null : numberOrNull(extraValue(priceList, 'partner_id'))
  const contractId = priceList === undefined ? null : numberOrNull(extraValue(priceList, 'contract_id'))
  const partnerLookup = useMasterSearchLookup('partners', partnerId === null ? [] : [partnerId])
  const contractLookup = useMasterSearchLookup('contracts', contractId === null ? [] : [contractId])

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
  const dateLabel = (key: string): string => {
    const value = priceList === undefined ? null : extraValue(priceList, key)
    return typeof value === 'string' ? formatDate(value, locale) : ''
  }
  const direction = priceList === undefined ? null : extraValue(priceList, 'direction')

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-4">
        <header className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-3">
            <Link
              to="/danh-muc-thiet-lap/danh-muc/bang-gia"
              className="flex items-center gap-1 text-sm text-secondary hover:underline"
            >
              <ArrowLeft size={14} aria-hidden />
              {t('priceList.back')}
            </Link>
            {priceList !== undefined && (
              <h1 className="text-lg font-semibold text-primary">
                {priceList.code} — {priceList.name}
              </h1>
            )}
            {priceList !== undefined && !priceList.is_active && (
              <StatusPill tone="todo">{t('catalog.status.inactive')}</StatusPill>
            )}
          </div>
          {priceList !== undefined && !readOnly && (
            <Button
              variant="secondary"
              onClick={() => {
                setEditing(true)
              }}
            >
              {t('priceList.edit')}
            </Button>
          )}
        </header>

        {!idInvalid && query.isPending && <p className="text-sm text-text-muted">{t('common.loading')}</p>}
        {error !== null && <Alert tone="error">{error}</Alert>}

        {priceList !== undefined && (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <section aria-label={t('priceList.info.title')} className="rounded border border-border-default bg-background p-4">
              <h2 className="mb-2 text-sm font-semibold text-primary">{t('priceList.info.title')}</h2>
              <dl className="flex flex-col gap-1.5">
                <InfoRow
                  label={t('catalog.field.priceDirection')}
                  value={
                    direction === 0 || direction === '0'
                      ? t('catalog.field.priceDirectionPurchase')
                      : direction === 1 || direction === '1'
                        ? t('catalog.field.priceDirectionSale')
                        : ''
                  }
                />
                <InfoRow label={t('catalog.field.effectiveFrom')} value={dateLabel('effective_from')} />
                <InfoRow label={t('catalog.field.effectiveTo')} value={dateLabel('effective_to')} />
                <InfoRow label={t('catalog.field.priceListPartner')} value={lookupLabel(partnerLookup, partnerId)} />
                <InfoRow label={t('catalog.field.priceListContract')} value={lookupLabel(contractLookup, contractId)} />
              </dl>
            </section>
            <div>
              {priceList.is_group ? (
                <Alert tone="info">{t('priceList.groupHasNoLines')}</Alert>
              ) : (
                <PriceListLinesCard priceListId={priceList.id} />
              )}
            </div>
          </div>
        )}

        {PRICE_LISTS_DEF !== undefined && priceList !== undefined && (
          <CatalogEditDrawer
            catalog={PRICE_LISTS_DEF}
            record={priceList}
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
