/**
 * Form tham số của một báo cáo — dựng từ spec server trả về (FR-RPT-002).
 *
 * Bộ chuẩn (`from_date`, `to_date`, `ledger`) là hợp đồng cố định nên vẽ sẵn;
 * ô `ledger` chỉ hiện khi `ledger_scope === 'both'` (definition ghim sổ thì
 * người dùng không có gì để chọn — BR-RPT-04). Các ô NGOÀI bộ chuẩn vẽ theo
 * `kind` server khai — thêm tham số cho một báo cáo là thêm dữ liệu, màn hình
 * này không đổi.
 *
 * `branch_ids` (thu hẹp chi nhánh) chưa có ô ở v1: RLS đã cắt đúng phạm vi
 * người gọi, thu hẹp thêm là tiện ích — chờ ô chọn chi nhánh dùng chung.
 */

import type { ReactElement } from 'react'

import { Seg, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { ReportParamField, ReportParams, ReportParamValues } from './use-reports'

export const LEDGER_FINANCIAL = '0'
export const LEDGER_MANAGEMENT = '1'

interface ReportParamsPanelProps {
  readonly spec: ReportParams
  readonly fromDate: string
  readonly toDate: string
  readonly ledger: string
  readonly extraValues: ReportParamValues
  readonly onFromDate: (value: string) => void
  readonly onToDate: (value: string) => void
  readonly onLedger: (value: string) => void
  readonly onExtraValue: (name: string, value: string | number | boolean) => void
}

function inputType(kind: ReportParamField['kind']): string {
  switch (kind) {
    case 'date':
      return 'date'
    case 'int':
    case 'decimal':
      return 'number'
    default:
      return 'text'
  }
}

export function ReportParamsPanel({
  spec,
  fromDate,
  toDate,
  ledger,
  extraValues,
  onFromDate,
  onToDate,
  onLedger,
  onExtraValue,
}: ReportParamsPanelProps): ReactElement {
  const { t } = useI18n()

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="w-40">
        <TextField
          label={t('reports.params.fromDate')}
          type="date"
          value={fromDate}
          onChange={(event) => {
            onFromDate(event.target.value)
          }}
        />
      </div>
      <div className="w-40">
        <TextField
          label={t('reports.params.toDate')}
          type="date"
          value={toDate}
          onChange={(event) => {
            onToDate(event.target.value)
          }}
        />
      </div>
      {spec.ledger_scope === 'both' ? (
        <Seg
          label={t('reports.params.ledger')}
          value={ledger}
          onChange={onLedger}
          options={[
            { value: LEDGER_FINANCIAL, label: t('reports.params.ledgerFinancial') },
            { value: LEDGER_MANAGEMENT, label: t('reports.params.ledgerManagement') },
          ]}
        />
      ) : null}
      {spec.params.map((field) =>
        field.kind === 'bool' ? (
          <label key={field.name} className="flex items-center gap-2 pb-2 text-sm">
            <input
              type="checkbox"
              checked={extraValues[field.name] === true}
              onChange={(event) => {
                onExtraValue(field.name, event.target.checked)
              }}
            />
            {field.label}
          </label>
        ) : (
          <div key={field.name} className="w-40">
            <TextField
              label={field.label}
              type={inputType(field.kind)}
              required={field.required}
              value={String(extraValues[field.name] ?? '')}
              onChange={(event) => {
                const raw = event.target.value
                onExtraValue(
                  field.name,
                  field.kind === 'int' && raw !== '' ? Number(raw) : raw,
                )
              }}
            />
          </div>
        ),
      )}
    </div>
  )
}
