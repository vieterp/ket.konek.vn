/**
 * Phần đầu form phiếu thu/chi (U2): thiết yếu hiện sẵn — Ngày hạch toán,
 * Nghiệp vụ (FR-SYS-025), TK quỹ, Đối tượng, Diễn giải; khối "Mở rộng" thu gọn
 * — Ngày chứng từ, Người nộp/nhận, Loại tiền, Tỷ giá, Hoạt động dòng tiền.
 *
 * Component KHÔNG giữ state — mọi giá trị/hàm đổi đi vào bằng prop, cùng khuôn
 * `so-sach-thue/journal-voucher-header-fields.tsx`.
 */

import type { ReactElement } from 'react'

import type { LookupOption } from '@/design-system/components'
import { AdvancedSection, LookupInput, SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { AutoPostingOperation } from './use-auto-posting'

export interface CashVoucherHeaderFieldsProps {
  readonly postingDate: string
  readonly onPostingDateChange: (value: string) => void
  readonly operationCode: string
  readonly operations: readonly AutoPostingOperation[]
  readonly onOperationChange: (value: string) => void
  readonly cashAccount: LookupOption | null
  readonly cashAccountOptions: readonly LookupOption[]
  readonly onCashAccountChange: (value: LookupOption | null) => void
  readonly partner: LookupOption | null
  readonly partnerOptions: readonly LookupOption[]
  readonly onPartnerChange: (value: LookupOption | null) => void
  readonly onPartnerQueryChange: (query: string) => void
  readonly partnerRequired: boolean
  readonly description: string
  readonly onDescriptionChange: (value: string) => void
  readonly documentDate: string
  readonly onDocumentDateChange: (value: string) => void
  readonly payerReceiverName: string
  readonly onPayerReceiverNameChange: (value: string) => void
  readonly currencyCode: string
  readonly onCurrencyCodeChange: (value: string) => void
  readonly exchangeRate: string
  readonly onExchangeRateChange: (value: string) => void
  readonly cashflowActivity: string
  readonly onCashflowActivityChange: (value: string) => void
  readonly defaultAdvancedOpen: boolean
}

export function CashVoucherHeaderFields(props: CashVoucherHeaderFieldsProps): ReactElement {
  const { t } = useI18n()

  return (
    <>
      <div className="flex flex-wrap gap-4">
        <TextField
          label={t('cashflow.form.postingDate')}
          type="date"
          value={props.postingDate}
          onChange={(event) => {
            props.onPostingDateChange(event.target.value)
          }}
        />
        <div className="min-w-[240px]">
          <SelectField
            label={t('cashflow.form.operation')}
            value={props.operationCode}
            onChange={(event) => {
              props.onOperationChange(event.target.value)
            }}
            options={[
              { value: '', label: t('cashflow.form.operationPlaceholder') },
              ...props.operations.map((operation) => ({
                value: operation.operation_code,
                label: operation.operation_name,
              })),
            ]}
          />
        </div>
        <div className="min-w-[200px]">
          <LookupInput
            label={t('cashflow.form.cashAccount')}
            value={props.cashAccount}
            onChange={props.onCashAccountChange}
            options={props.cashAccountOptions}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('cashflow.form.lookupEmpty')}
          />
        </div>
        <div className="min-w-[240px]">
          <LookupInput
            label={
              props.partnerRequired
                ? t('cashflow.form.partnerRequired')
                : t('cashflow.form.partner')
            }
            value={props.partner}
            onChange={props.onPartnerChange}
            options={props.partnerOptions}
            onQueryChange={props.onPartnerQueryChange}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('cashflow.form.lookupEmpty')}
          />
        </div>
        <div className="min-w-[280px] flex-1">
          <TextField
            label={t('cashflow.form.description')}
            value={props.description}
            onChange={(event) => {
              props.onDescriptionChange(event.target.value)
            }}
          />
        </div>
      </div>

      <AdvancedSection label={t('cashflow.form.advanced')} defaultOpen={props.defaultAdvancedOpen}>
        <div className="flex flex-wrap gap-4">
          <TextField
            label={t('cashflow.form.documentDate')}
            type="date"
            value={props.documentDate}
            onChange={(event) => {
              props.onDocumentDateChange(event.target.value)
            }}
          />
          <TextField
            label={t('cashflow.form.payerReceiverName')}
            value={props.payerReceiverName}
            onChange={(event) => {
              props.onPayerReceiverNameChange(event.target.value)
            }}
          />
          <TextField
            label={t('cashflow.form.currencyCode')}
            value={props.currencyCode}
            onChange={(event) => {
              props.onCurrencyCodeChange(event.target.value)
            }}
          />
          <TextField
            label={t('cashflow.form.exchangeRate')}
            inputMode="decimal"
            value={props.exchangeRate}
            onChange={(event) => {
              props.onExchangeRateChange(event.target.value)
            }}
          />
          <TextField
            label={t('cashflow.form.cashflowActivity')}
            inputMode="numeric"
            value={props.cashflowActivity}
            onChange={(event) => {
              props.onCashflowActivityChange(event.target.value)
            }}
          />
        </div>
      </AdvancedSection>
    </>
  )
}
