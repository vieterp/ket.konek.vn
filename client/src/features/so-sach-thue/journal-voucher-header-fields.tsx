/**
 * Phần đầu form chứng từ (U2): ba thứ thiết yếu hiện sẵn (Ngày hạch toán,
 * Diễn giải) + khối "Mở rộng" thu gọn (Ngày chứng từ, Loại tiền, Tỷ giá, hoạt
 * động dòng tiền). Lưới chi tiết là thứ ba, vẽ riêng ở `journal-voucher-form.tsx`.
 *
 * Component KHÔNG giữ state — mọi giá trị/hàm đổi giá trị đi vào bằng prop, để
 * `VoucherFormBody` là nơi duy nhất biết "Ngày chứng từ" có đang tự đồng bộ
 * theo "Ngày hạch toán" hay đã bị người dùng tự tay sửa.
 */

import type { ReactElement } from 'react'

import { AdvancedSection, SelectField, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

export interface JournalVoucherHeaderFieldsProps {
  readonly postingDate: string
  readonly onPostingDateChange: (value: string) => void
  readonly description: string
  readonly onDescriptionChange: (value: string) => void
  readonly documentDate: string
  readonly onDocumentDateChange: (value: string) => void
  readonly currencyCode: string
  readonly onCurrencyCodeChange: (value: string) => void
  readonly exchangeRate: string
  readonly onExchangeRateChange: (value: string) => void
  readonly cashflowActivity: string
  readonly onCashflowActivityChange: (value: string) => void
  readonly entryKind: string
  readonly onEntryKindChange: (value: string) => void
  /** Mở sẵn khối "Mở rộng" khi form sửa đã có giá trị khác mặc định. */
  readonly defaultAdvancedOpen: boolean
}

export function JournalVoucherHeaderFields({
  postingDate,
  onPostingDateChange,
  description,
  onDescriptionChange,
  documentDate,
  onDocumentDateChange,
  currencyCode,
  onCurrencyCodeChange,
  exchangeRate,
  onExchangeRateChange,
  cashflowActivity,
  onCashflowActivityChange,
  entryKind,
  onEntryKindChange,
  defaultAdvancedOpen,
}: JournalVoucherHeaderFieldsProps): ReactElement {
  const { t } = useI18n()

  return (
    <>
      <div className="flex flex-wrap gap-4">
        <TextField
          label={t('gl.form.postingDate')}
          type="date"
          value={postingDate}
          onChange={(event) => {
            onPostingDateChange(event.target.value)
          }}
        />
        <div className="min-w-[280px] flex-1">
          <TextField
            label={t('gl.form.description')}
            value={description}
            onChange={(event) => {
              onDescriptionChange(event.target.value)
            }}
          />
        </div>
      </div>

      <AdvancedSection label={t('gl.form.advanced')} defaultOpen={defaultAdvancedOpen}>
        <div className="flex flex-wrap gap-4">
          <TextField
            label={t('gl.form.documentDate')}
            type="date"
            value={documentDate}
            onChange={(event) => {
              onDocumentDateChange(event.target.value)
            }}
          />
          <TextField
            label={t('gl.form.currencyCode')}
            value={currencyCode}
            onChange={(event) => {
              onCurrencyCodeChange(event.target.value)
            }}
          />
          <TextField
            label={t('gl.form.exchangeRate')}
            inputMode="decimal"
            value={exchangeRate}
            onChange={(event) => {
              onExchangeRateChange(event.target.value)
            }}
          />
          <TextField
            label={t('gl.form.cashflowActivity')}
            inputMode="numeric"
            value={cashflowActivity}
            onChange={(event) => {
              onCashflowActivityChange(event.target.value)
            }}
          />
          {/* Bản chất bút toán (LD-17): kết chuyển bị loại khỏi phát sinh của
              báo cáo kết quả kinh doanh. Đây là màn hình duy nhất khai được cờ
              cho tới khi 10a có engine kết chuyển tự động. */}
          <SelectField
            label={t('gl.form.entryKind')}
            value={entryKind}
            options={[
              { value: '0', label: t('gl.form.entryKind.operating') },
              { value: '1', label: t('gl.form.entryKind.closing') },
            ]}
            onChange={(event) => {
              onEntryKindChange(event.target.value)
            }}
          />
        </div>
      </AdvancedSection>
    </>
  )
}
