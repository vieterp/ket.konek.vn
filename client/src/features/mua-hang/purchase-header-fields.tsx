/**
 * Phần đầu form hóa đơn mua (U2, design `#mua-form`): thẻ "Mua của ai" với
 * đúng BA trường bắt buộc — Nhà cung cấp, Ngày, Nghiệp vụ (FR-SYS-025) — và
 * khối "Mở rộng" thu gọn: hóa đơn NCC, TK phải trả, điều khoản/hạn trả, tiền
 * tệ/tỷ giá, người mua, diễn giải, ngày chứng từ.
 *
 * Design vẽ trường thứ ba là "Lấy từ đơn mua hàng"; chưa có phân hệ đơn hàng
 * nên chỗ ấy là Nghiệp vụ — thứ server bắt buộc và thứ điền sẵn TK cho dòng.
 * Component KHÔNG giữ state — mọi giá trị đi vào bằng prop. Không khóa ô khi
 * chứng từ đã ghi sổ — cùng khuôn form phiếu quỹ: server từ chối PUT, footer
 * đã ẩn nút Cất.
 */

import type { ReactElement } from 'react'

import type { LookupOption } from '@/design-system/components'
import { AdvancedSection, LookupInput, Seg, SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { AutoPostingOperation } from '@/features/tien-vao-tien-ra/use-auto-posting'

import { FormCard } from './form-card'
import {
  VENDOR_INVOICE_NONE,
  VENDOR_INVOICE_NOT_YET,
  VENDOR_INVOICE_RECEIVED,
} from './purchase-row-status'

export interface PurchaseHeaderFieldsProps {
  readonly vendor: LookupOption | null
  readonly vendorOptions: readonly LookupOption[]
  readonly onVendorChange: (value: LookupOption | null) => void
  readonly onVendorQueryChange: (query: string) => void
  /** "MST … · điều khoản …" dưới ô NCC; `null` khi chưa chọn hoặc chưa tải. */
  readonly vendorHint: string | null
  readonly postingDate: string
  readonly onPostingDateChange: (value: string) => void
  readonly operationCode: string
  readonly operations: readonly AutoPostingOperation[]
  readonly onOperationChange: (value: string) => void

  readonly vendorInvoiceStatus: number
  readonly onVendorInvoiceStatusChange: (value: number) => void
  readonly vendorInvoiceForm: string
  readonly onVendorInvoiceFormChange: (value: string) => void
  readonly vendorInvoiceSerial: string
  readonly onVendorInvoiceSerialChange: (value: string) => void
  readonly vendorInvoiceNo: string
  readonly onVendorInvoiceNoChange: (value: string) => void
  readonly vendorInvoiceDate: string
  readonly onVendorInvoiceDateChange: (value: string) => void
  readonly payableAccount: LookupOption | null
  readonly payableAccountOptions: readonly LookupOption[]
  readonly onPayableAccountChange: (value: LookupOption | null) => void
  readonly paymentTermId: string
  readonly paymentTermOptions: readonly LookupOption[]
  readonly onPaymentTermChange: (value: string) => void
  readonly dueDate: string
  readonly onDueDateChange: (value: string) => void
  readonly buyer: LookupOption | null
  readonly buyerOptions: readonly LookupOption[]
  readonly onBuyerChange: (value: LookupOption | null) => void
  readonly onBuyerQueryChange: (query: string) => void
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

export function PurchaseHeaderFields(props: PurchaseHeaderFieldsProps): ReactElement {
  const { t } = useI18n()
  const received = props.vendorInvoiceStatus === VENDOR_INVOICE_RECEIVED

  return (
    <>
      <FormCard title={t('purchase.form.card.who')} aside={t('purchase.form.threeRequired')}>
        <div className="grid gap-3.5 p-3.5 md:grid-cols-[1.4fr_1fr_1fr]">
          <LookupInput
            label={t('purchase.form.vendor')}
            value={props.vendor}
            onChange={props.onVendorChange}
            options={props.vendorOptions}
            onQueryChange={props.onVendorQueryChange}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('purchase.form.lookupEmpty')}
            {...(props.vendorHint === null ? {} : { hint: props.vendorHint })}
          />
          <TextField
            label={t('purchase.form.postingDate')}
            type="date"
            value={props.postingDate}
            onChange={(event) => {
              props.onPostingDateChange(event.target.value)
            }}
          />
          <SelectField
            label={t('purchase.form.operation')}
            value={props.operationCode}
            onChange={(event) => {
              props.onOperationChange(event.target.value)
            }}
            options={[
              { value: '', label: t('purchase.form.operationPlaceholder') },
              ...props.operations.map((operation) => ({
                value: operation.operation_code,
                label: operation.operation_name,
              })),
            ]}
          />
        </div>
      </FormCard>

      <AdvancedSection label={t('purchase.form.advanced')} defaultOpen={props.defaultAdvancedOpen}>
        <div className="flex flex-col gap-4">
          <fieldset className="flex flex-wrap items-end gap-4">
            <legend className="mb-2 text-sm font-semibold text-primary">
              {t('purchase.form.vendorInvoice.legend')}
            </legend>
            <div className="min-w-[280px]">
              <Seg
                label={t('purchase.form.vendorInvoice.status')}
                value={String(props.vendorInvoiceStatus)}
                onChange={(value) => {
                  props.onVendorInvoiceStatusChange(Number.parseInt(value, 10))
                }}
                options={[
                  { value: String(VENDOR_INVOICE_RECEIVED), label: t('purchase.form.vendorInvoice.received') },
                  { value: String(VENDOR_INVOICE_NOT_YET), label: t('purchase.form.vendorInvoice.notYet') },
                  { value: String(VENDOR_INVOICE_NONE), label: t('purchase.form.vendorInvoice.none') },
                ]}
              />
            </div>
            {received && (
              <>
                <TextField
                  label={t('purchase.form.vendorInvoice.form')}
                  value={props.vendorInvoiceForm}
                  onChange={(event) => {
                    props.onVendorInvoiceFormChange(event.target.value)
                  }}
                />
                <TextField
                  label={t('purchase.form.vendorInvoice.serial')}
                  value={props.vendorInvoiceSerial}
                  onChange={(event) => {
                    props.onVendorInvoiceSerialChange(event.target.value)
                  }}
                />
                <TextField
                  label={t('purchase.form.vendorInvoice.no')}
                  value={props.vendorInvoiceNo}
                  onChange={(event) => {
                    props.onVendorInvoiceNoChange(event.target.value)
                  }}
                />
                <TextField
                  label={t('purchase.form.vendorInvoice.date')}
                  type="date"
                  value={props.vendorInvoiceDate}
                  onChange={(event) => {
                    props.onVendorInvoiceDateChange(event.target.value)
                  }}
                />
              </>
            )}
          </fieldset>

          <div className="flex flex-wrap gap-4">
            <div className="min-w-[240px]">
              <LookupInput
                label={t('purchase.form.payableAccount')}
                value={props.payableAccount}
                onChange={props.onPayableAccountChange}
                options={props.payableAccountOptions}
                clearLabel={t('catalog.lookup.clear')}
                emptyLabel={t('purchase.form.lookupEmpty')}
              />
            </div>
            <div className="min-w-[200px]">
              <SelectField
                label={t('purchase.form.paymentTerm')}
                value={props.paymentTermId}
                onChange={(event) => {
                  props.onPaymentTermChange(event.target.value)
                }}
                options={[
                  { value: '', label: t('purchase.form.paymentTermPlaceholder') },
                  ...props.paymentTermOptions.map((term) => ({
                    value: String(term.id),
                    label: term.label,
                  })),
                ]}
              />
            </div>
            <TextField
              label={t('purchase.form.dueDate')}
              type="date"
              value={props.dueDate}
              hint={t('purchase.form.dueDateHint')}
              onChange={(event) => {
                props.onDueDateChange(event.target.value)
              }}
            />
            <div className="min-w-[240px]">
              <LookupInput
                label={t('purchase.form.buyer')}
                value={props.buyer}
                onChange={props.onBuyerChange}
                options={props.buyerOptions}
                onQueryChange={props.onBuyerQueryChange}
                clearLabel={t('catalog.lookup.clear')}
                emptyLabel={t('purchase.form.lookupEmpty')}
              />
            </div>
          </div>

          <div className="flex flex-wrap gap-4">
            <TextField
              label={t('purchase.form.documentDate')}
              type="date"
              value={props.documentDate}
              onChange={(event) => {
                props.onDocumentDateChange(event.target.value)
              }}
            />
            <TextField
              label={t('purchase.form.currencyCode')}
              value={props.currencyCode}
              onChange={(event) => {
                props.onCurrencyCodeChange(event.target.value)
              }}
            />
            <TextField
              label={t('purchase.form.exchangeRate')}
              inputMode="decimal"
              value={props.exchangeRate}
              onChange={(event) => {
                props.onExchangeRateChange(event.target.value)
              }}
            />
            <div className="min-w-[280px] flex-1">
              <TextField
                label={t('purchase.form.description')}
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
