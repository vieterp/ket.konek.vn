/**
 * Phần đầu form hóa đơn bán (U2, bộ xương `#mua-form` dùng lại): thẻ "Bán cho
 * ai" với đúng BA trường bắt buộc — Khách hàng, Ngày, Nghiệp vụ (FR-SYS-025) —
 * và khối "Mở rộng" thu gọn: hóa đơn gõ tay (FR-SAL-004), TK phải thu, điều
 * khoản/hạn thu, bảng giá ép, nhân viên bán hàng, người nhận / địa chỉ giao,
 * cờ xuất kho, tiền tệ/tỷ giá, diễn giải, ngày chứng từ.
 *
 * Component KHÔNG giữ state — mọi giá trị đi vào bằng prop. Không khóa ô khi
 * chứng từ đã ghi sổ — cùng khuôn form phiếu quỹ: server từ chối PUT, footer
 * đã ẩn nút Cất.
 */

import type { ReactElement } from 'react'

import type { LookupOption } from '@/design-system/components'
import { AdvancedSection, LookupInput, Seg, SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import { FormCard } from '@/features/mua-hang/form-card'
import type { AutoPostingOperation } from '@/features/tien-vao-tien-ra/use-auto-posting'

export interface SalesHeaderFieldsProps {
  readonly customer: LookupOption | null
  readonly customerOptions: readonly LookupOption[]
  readonly onCustomerChange: (value: LookupOption | null) => void
  readonly onCustomerQueryChange: (query: string) => void
  /** "MST … · điều khoản …" dưới ô khách; `null` khi chưa chọn hoặc chưa tải. */
  readonly customerHint: string | null
  readonly postingDate: string
  readonly onPostingDateChange: (value: string) => void
  readonly operationCode: string
  readonly operations: readonly AutoPostingOperation[]
  readonly onOperationChange: (value: string) => void

  readonly invoiceForm: string
  readonly onInvoiceFormChange: (value: string) => void
  readonly invoiceSerial: string
  readonly onInvoiceSerialChange: (value: string) => void
  readonly invoiceNo: string
  readonly onInvoiceNoChange: (value: string) => void
  readonly invoiceDate: string
  readonly onInvoiceDateChange: (value: string) => void
  readonly receivableAccount: LookupOption | null
  readonly receivableAccountOptions: readonly LookupOption[]
  readonly onReceivableAccountChange: (value: LookupOption | null) => void
  readonly paymentTermId: string
  readonly paymentTermOptions: readonly LookupOption[]
  readonly onPaymentTermChange: (value: string) => void
  readonly dueDate: string
  readonly onDueDateChange: (value: string) => void
  readonly priceList: LookupOption | null
  readonly priceListOptions: readonly LookupOption[]
  readonly onPriceListChange: (value: LookupOption | null) => void
  readonly salesperson: LookupOption | null
  readonly salespersonOptions: readonly LookupOption[]
  readonly onSalespersonChange: (value: LookupOption | null) => void
  readonly onSalespersonQueryChange: (query: string) => void
  readonly recipientName: string
  readonly onRecipientNameChange: (value: string) => void
  readonly shipTo: string
  readonly onShipToChange: (value: string) => void
  readonly isStockIssue: boolean
  readonly onIsStockIssueChange: (value: boolean) => void
  readonly documentDate: string
  readonly onDocumentDateChange: (value: string) => void
  readonly currencyCode: string
  readonly onCurrencyCodeChange: (value: string) => void
  readonly exchangeRate: string
  readonly onExchangeRateChange: (value: string) => void
  readonly description: string
  readonly onDescriptionChange: (value: string) => void
  readonly defaultAdvancedOpen: boolean
}

export function SalesHeaderFields(props: SalesHeaderFieldsProps): ReactElement {
  const { t } = useI18n()

  return (
    <>
      <FormCard title={t('sales.form.card.who')} aside={t('sales.form.threeRequired')}>
        <div className="grid gap-3.5 p-3.5 md:grid-cols-[1.4fr_1fr_1fr]">
          <LookupInput
            label={t('sales.form.customer')}
            value={props.customer}
            onChange={props.onCustomerChange}
            options={props.customerOptions}
            onQueryChange={props.onCustomerQueryChange}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('sales.form.lookupEmpty')}
            {...(props.customerHint === null ? {} : { hint: props.customerHint })}
          />
          <TextField
            label={t('sales.form.postingDate')}
            type="date"
            value={props.postingDate}
            onChange={(event) => {
              props.onPostingDateChange(event.target.value)
            }}
          />
          <SelectField
            label={t('sales.form.operation')}
            value={props.operationCode}
            onChange={(event) => {
              props.onOperationChange(event.target.value)
            }}
            options={[
              { value: '', label: t('sales.form.operationPlaceholder') },
              ...props.operations.map((operation) => ({
                value: operation.operation_code,
                label: operation.operation_name,
              })),
            ]}
          />
        </div>
      </FormCard>

      <AdvancedSection label={t('sales.form.advanced')} defaultOpen={props.defaultAdvancedOpen}>
        <div className="flex flex-col gap-4">
          <fieldset className="flex flex-wrap items-end gap-4">
            <legend className="mb-2 text-sm font-semibold text-primary">
              {t('sales.form.invoice.legend')}
            </legend>
            <TextField
              label={t('sales.form.invoice.form')}
              value={props.invoiceForm}
              onChange={(event) => {
                props.onInvoiceFormChange(event.target.value)
              }}
            />
            <TextField
              label={t('sales.form.invoice.serial')}
              value={props.invoiceSerial}
              onChange={(event) => {
                props.onInvoiceSerialChange(event.target.value)
              }}
            />
            <TextField
              label={t('sales.form.invoice.no')}
              value={props.invoiceNo}
              onChange={(event) => {
                props.onInvoiceNoChange(event.target.value)
              }}
            />
            <TextField
              label={t('sales.form.invoice.date')}
              type="date"
              value={props.invoiceDate}
              onChange={(event) => {
                props.onInvoiceDateChange(event.target.value)
              }}
            />
            <p className="basis-full text-xs text-text-muted">{t('sales.form.invoice.hint')}</p>
          </fieldset>

          <div className="flex flex-wrap gap-4">
            <div className="min-w-[240px]">
              <LookupInput
                label={t('sales.form.receivableAccount')}
                value={props.receivableAccount}
                onChange={props.onReceivableAccountChange}
                options={props.receivableAccountOptions}
                clearLabel={t('catalog.lookup.clear')}
                emptyLabel={t('sales.form.lookupEmpty')}
              />
            </div>
            <div className="min-w-[200px]">
              <SelectField
                label={t('sales.form.paymentTerm')}
                value={props.paymentTermId}
                onChange={(event) => {
                  props.onPaymentTermChange(event.target.value)
                }}
                options={[
                  { value: '', label: t('sales.form.paymentTermPlaceholder') },
                  ...props.paymentTermOptions.map((term) => ({
                    value: String(term.id),
                    label: term.label,
                  })),
                ]}
              />
            </div>
            <TextField
              label={t('sales.form.dueDate')}
              type="date"
              value={props.dueDate}
              hint={t('sales.form.dueDateHint')}
              onChange={(event) => {
                props.onDueDateChange(event.target.value)
              }}
            />
            <div className="min-w-[240px]">
              <LookupInput
                label={t('sales.form.priceList')}
                value={props.priceList}
                onChange={props.onPriceListChange}
                options={props.priceListOptions}
                clearLabel={t('catalog.lookup.clear')}
                emptyLabel={t('sales.form.lookupEmpty')}
                hint={t('sales.form.priceListHint')}
              />
            </div>
            <div className="min-w-[240px]">
              <LookupInput
                label={t('sales.form.salesperson')}
                value={props.salesperson}
                onChange={props.onSalespersonChange}
                options={props.salespersonOptions}
                onQueryChange={props.onSalespersonQueryChange}
                clearLabel={t('catalog.lookup.clear')}
                emptyLabel={t('sales.form.lookupEmpty')}
              />
            </div>
          </div>

          <div className="flex flex-wrap items-end gap-4">
            <TextField
              label={t('sales.form.recipientName')}
              value={props.recipientName}
              onChange={(event) => {
                props.onRecipientNameChange(event.target.value)
              }}
            />
            <div className="min-w-[280px] flex-1">
              <TextField
                label={t('sales.form.shipTo')}
                value={props.shipTo}
                onChange={(event) => {
                  props.onShipToChange(event.target.value)
                }}
              />
            </div>
            <div className="min-w-[200px]">
              <Seg
                label={t('sales.form.stockIssue')}
                value={props.isStockIssue ? 'yes' : 'no'}
                onChange={(value) => {
                  props.onIsStockIssueChange(value === 'yes')
                }}
                options={[
                  { value: 'yes', label: t('sales.form.stockIssue.yes') },
                  { value: 'no', label: t('sales.form.stockIssue.no') },
                ]}
              />
            </div>
          </div>

          <div className="flex flex-wrap gap-4">
            <TextField
              label={t('sales.form.documentDate')}
              type="date"
              value={props.documentDate}
              onChange={(event) => {
                props.onDocumentDateChange(event.target.value)
              }}
            />
            <TextField
              label={t('sales.form.currencyCode')}
              value={props.currencyCode}
              onChange={(event) => {
                props.onCurrencyCodeChange(event.target.value)
              }}
            />
            <TextField
              label={t('sales.form.exchangeRate')}
              inputMode="decimal"
              value={props.exchangeRate}
              onChange={(event) => {
                props.onExchangeRateChange(event.target.value)
              }}
            />
            <div className="min-w-[280px] flex-1">
              <TextField
                label={t('sales.form.description')}
                value={props.description}
                onChange={(event) => {
                  props.onDescriptionChange(event.target.value)
                }}
              />
            </div>
          </div>
        </div>
      </AdvancedSection>
    </>
  )
}
