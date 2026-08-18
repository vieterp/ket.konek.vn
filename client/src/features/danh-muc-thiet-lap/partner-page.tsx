/**
 * Màn hình chi tiết đối tác — danh mục dùng chung cho cả mua và bán (nhóm 07).
 *
 * Ba thẻ: thông tin danh mục, tài khoản ngân hàng (FR-SYS-033), công nợ (giữ
 * chỗ tới phase 7 — H56). Đọc thẳng router module chứ không BFF, cùng lý do.
 *
 * Nút "Sửa" mở lại đúng `CatalogEditDrawer` của danh mục đối tác: một form cho
 * một việc, dù đi vào từ danh sách hay từ trang chi tiết.
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
import { FeatureNav } from './feature-nav'
import { PartnerBankAccountsCard } from './partner-bank-accounts-card'
import { PartnerDebtCard } from './partner-debt-card'
import type { Partner } from './use-partner'
import { usePartner } from './use-partner'

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

export function PartnerPage(): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const { id } = useParams()
  const partnerId = Number.parseInt(id ?? '', 10)
  const idInvalid = Number.isNaN(partnerId)
  const query = usePartner(idInvalid ? null : partnerId)
  const [editing, setEditing] = useState(false)

  const partnersDef = catalogBySlug('partners')
  const partner: Partner | undefined = query.data

  // Đường dẫn gõ tay `/doi-tac/abc` phải ra thông báo, không phải vòng chờ
  // vĩnh viễn (truy vấn bị tắt thì `isPending` đứng yên mãi).
  const error = idInvalid
    ? translateErrorCode(t, 'master_data.not_found')
    : query.error instanceof ApiError
      ? translateErrorCode(t, query.error.errorCode)
      : query.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-4">
        <header className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-3">
            <Link
              to="/danh-muc-thiet-lap/danh-muc/doi-tac"
              className="flex items-center gap-1 text-sm text-secondary hover:underline"
            >
              <ArrowLeft size={14} aria-hidden />
              {t('partner.back')}
            </Link>
            {partner !== undefined && (
              <h1 className="text-lg font-semibold text-primary">
                {partner.code} — {partner.name}
              </h1>
            )}
            {partner !== undefined && !partner.is_active && (
              <StatusPill tone="todo">{t('catalog.status.inactive')}</StatusPill>
            )}
          </div>
          {partner !== undefined && !readOnly && (
            <Button
              variant="secondary"
              onClick={() => {
                setEditing(true)
              }}
            >
              {t('partner.edit')}
            </Button>
          )}
        </header>

        {!idInvalid && query.isPending && (
          <p className="text-sm text-text-muted">{t('common.loading')}</p>
        )}
        {error !== null && <Alert tone="error">{error}</Alert>}

        {partner !== undefined && (
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <section
              aria-label={t('partner.info.title')}
              className="rounded border border-border-default bg-background p-4"
            >
              <h2 className="mb-2 text-sm font-semibold text-primary">
                {t('partner.info.title')}
              </h2>
              <dl className="flex flex-col gap-1.5">
                <InfoRow
                  label={t('partner.info.roles')}
                  value={[
                    partner.is_customer === true ? t('catalog.flag.customer') : '',
                    partner.is_vendor === true ? t('catalog.flag.vendor') : '',
                  ]
                    .filter((role) => role !== '')
                    .join(' · ')}
                />
                <InfoRow
                  label={t('catalog.field.isOrganization')}
                  value={
                    partner.is_organization === true
                      ? t('partner.info.organization')
                      : t('partner.info.individual')
                  }
                />
                <InfoRow label={t('catalog.field.taxCode')} value={partner.tax_code ?? ''} />
                <InfoRow label={t('catalog.field.address')} value={partner.address ?? ''} />
                <InfoRow label={t('catalog.field.province')} value={partner.province ?? ''} />
                <InfoRow label={t('catalog.field.contactName')} value={partner.contact_name ?? ''} />
                <InfoRow label={t('catalog.field.phone')} value={partner.phone ?? ''} />
                <InfoRow label={t('catalog.field.email')} value={partner.email ?? ''} />
                <InfoRow label={t('catalog.field.website')} value={partner.website ?? ''} />
                <InfoRow
                  label={t('catalog.field.invoiceRecipient')}
                  value={partner.invoice_recipient ?? ''}
                />
                <InfoRow
                  label={t('catalog.field.invoiceEmail')}
                  value={partner.invoice_email ?? ''}
                />
                <InfoRow
                  label={t('catalog.field.creditLimit')}
                  value={partner.credit_limit ?? ''}
                />
              </dl>
            </section>

            <div className="flex flex-col gap-4">
              <PartnerDebtCard />
              <PartnerBankAccountsCard partnerId={partner.id} />
            </div>
          </div>
        )}

        {partnersDef !== undefined && partner !== undefined && (
          <CatalogEditDrawer
            catalog={partnersDef}
            record={partner}
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
