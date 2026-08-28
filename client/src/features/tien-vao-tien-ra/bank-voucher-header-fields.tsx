/**
 * Phần đầu form chứng từ tiền gửi (U2): Loại chứng từ (Seg 4 loại — bất biến
 * sau khi cất), Ngày hạch toán, Nghiệp vụ (ẩn với chuyển nội bộ — CTNB không
 * có nghiệp vụ định khoản), TK ngân hàng, TK đích (chỉ CTNB), Đối tượng, Diễn
 * giải; khối "Mở rộng" — Ngày chứng từ, Người thụ hưởng (UNC/SEC), Số/Ngày séc
 * (SEC), Số tham chiếu, Loại tiền, Tỷ giá, Hoạt động dòng tiền.
 */

import type { ReactElement } from 'react'

import type { LookupOption } from '@/design-system/components'
import { AdvancedSection, LookupInput, Seg, SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { AutoPostingOperation } from './use-auto-posting'

export const BANK_KIND_CREDIT_ADVICE = 0
export const BANK_KIND_PAYMENT_ORDER = 1
export const BANK_KIND_CHEQUE = 2
export const BANK_KIND_INTERNAL_TRANSFER = 3

export interface BankVoucherHeaderFieldsProps {
  readonly kind: number
  readonly onKindChange: (value: number) => void
  readonly kindLocked: boolean
  readonly postingDate: string
  readonly onPostingDateChange: (value: string) => void
  readonly operationCode: string
  readonly operations: readonly AutoPostingOperation[]
  readonly onOperationChange: (value: string) => void
  readonly bankAccount: LookupOption | null
  readonly bankAccountOptions: readonly LookupOption[]
  readonly onBankAccountChange: (value: LookupOption | null) => void
  readonly counterBankAccount: LookupOption | null
  readonly onCounterBankAccountChange: (value: LookupOption | null) => void
  readonly partner: LookupOption | null
  readonly partnerOptions: readonly LookupOption[]
  readonly onPartnerChange: (value: LookupOption | null) => void
  readonly onPartnerQueryChange: (query: string) => void
  readonly partnerRequired: boolean
  readonly description: string
  readonly onDescriptionChange: (value: string) => void
  readonly documentDate: string
  readonly onDocumentDateChange: (value: string) => void
  readonly beneficiaryName: string
  readonly onBeneficiaryNameChange: (value: string) => void
  readonly beneficiaryAccountNo: string
  readonly onBeneficiaryAccountNoChange: (value: string) => void
  readonly beneficiaryBankName: string
  readonly onBeneficiaryBankNameChange: (value: string) => void
  readonly chequeNo: string
  readonly onChequeNoChange: (value: string) => void
  readonly chequeDate: string
  readonly onChequeDateChange: (value: string) => void
  readonly referenceNo: string
  readonly onReferenceNoChange: (value: string) => void
  readonly currencyCode: string
  readonly onCurrencyCodeChange: (value: string) => void
  readonly exchangeRate: string
  readonly onExchangeRateChange: (value: string) => void
  readonly cashflowActivity: string
  readonly onCashflowActivityChange: (value: string) => void
  readonly defaultAdvancedOpen: boolean
}

export function BankVoucherHeaderFields(props: BankVoucherHeaderFieldsProps): ReactElement {
  const { t } = useI18n()
  const transfer = props.kind === BANK_KIND_INTERNAL_TRANSFER
  const payment = props.kind === BANK_KIND_PAYMENT_ORDER || props.kind === BANK_KIND_CHEQUE

  return (
    <>
      {!props.kindLocked && (
        <Seg
          label={t('cashflow.bank.kindLabel')}
          options={[
            { value: String(BANK_KIND_CREDIT_ADVICE), label: t('cashflow.bank.kind.creditAdvice') },
            { value: String(BANK_KIND_PAYMENT_ORDER), label: t('cashflow.bank.kind.paymentOrder') },
            { value: String(BANK_KIND_CHEQUE), label: t('cashflow.bank.kind.cheque') },
            {
              value: String(BANK_KIND_INTERNAL_TRANSFER),
              label: t('cashflow.bank.kind.internalTransfer'),
            },
          ]}
          value={String(props.kind)}
          onChange={(value) => {
            props.onKindChange(Number.parseInt(value, 10))
          }}
        />
      )}

      <div className="flex flex-wrap gap-4">
        <TextField
          label={t('cashflow.form.postingDate')}
          type="date"
          value={props.postingDate}
          onChange={(event) => {
            props.onPostingDateChange(event.target.value)
          }}
        />
        {!transfer && (
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
        )}
        <div className="min-w-[220px]">
          <LookupInput
            label={t('cashflow.bank.account')}
            value={props.bankAccount}
            onChange={props.onBankAccountChange}
            options={props.bankAccountOptions}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('cashflow.form.lookupEmpty')}
          />
        </div>
        {transfer && (
          <div className="min-w-[220px]">
            <LookupInput
              label={t('cashflow.bank.counterAccount')}
              value={props.counterBankAccount}
              onChange={props.onCounterBankAccountChange}
              options={props.bankAccountOptions}
              clearLabel={t('catalog.lookup.clear')}
              emptyLabel={t('cashflow.form.lookupEmpty')}
            />
          </div>
        )}
        {!transfer && (
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
        )}
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
          {payment && (
            <>
              <TextField
                label={t('cashflow.bank.beneficiaryName')}
                value={props.beneficiaryName}
                onChange={(event) => {
                  props.onBeneficiaryNameChange(event.target.value)
                }}
              />
              <TextField
                label={t('cashflow.bank.beneficiaryAccountNo')}
                value={props.beneficiaryAccountNo}
                onChange={(event) => {
                  props.onBeneficiaryAccountNoChange(event.target.value)
                }}
              />
              <TextField
                label={t('cashflow.bank.beneficiaryBankName')}
                value={props.beneficiaryBankName}
                onChange={(event) => {
                  props.onBeneficiaryBankNameChange(event.target.value)
                }}
              />
            </>
          )}
          {props.kind === BANK_KIND_CHEQUE && (
            <>
              <TextField
                label={t('cashflow.bank.chequeNo')}
                value={props.chequeNo}
                onChange={(event) => {
                  props.onChequeNoChange(event.target.value)
                }}
              />
              <TextField
                label={t('cashflow.bank.chequeDate')}
                type="date"
                value={props.chequeDate}
                onChange={(event) => {
                  props.onChequeDateChange(event.target.value)
                }}
              />
            </>
          )}
          <TextField
            label={t('cashflow.bank.referenceNo')}
            value={props.referenceNo}
            onChange={(event) => {
              props.onReferenceNoChange(event.target.value)
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
