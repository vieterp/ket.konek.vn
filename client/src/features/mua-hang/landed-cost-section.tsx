/**
 * Khối "Chi phí mua hàng" dưới lưới hàng (design `#mua-form`, FR-PUR §4.2):
 * lưới chi phí (vận chuyển, bốc xếp…) + cách phân bổ vào giá nhập — theo giá
 * trị / theo số lượng / thủ công. Hai cách đầu server tính và ghi vào cột
 * "Chi phí mua" của từng dòng (chỉ-đọc); thủ công thì cột ấy mở khóa và tổng
 * phải khớp (server phán). Lồng trong thân hóa đơn — không có chứng từ riêng.
 */

import type { ReactElement } from 'react'

import type { DataGridChange, LookupOption } from '@/design-system/components'
import { DataGrid, Seg } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import { buildLandedCostColumns } from './purchase-line-columns'
import type { LandedCostRow } from './purchase-line-types'

/** Khớp `LandedCostAllocation` phía server. */
export const ALLOCATION_BY_VALUE = 0
export const ALLOCATION_BY_QUANTITY = 1
export const ALLOCATION_MANUAL = 2

/** Ba dòng chi phí là đủ cho hầu hết hóa đơn — lưới thấp, không chiếm màn hình. */
const COST_GRID_HEIGHT = 140

export function LandedCostSection({
  rows,
  onCommit,
  allocation,
  onAllocationChange,
  accounts,
  vendorOptions,
}: {
  readonly rows: readonly LandedCostRow[]
  readonly onCommit: (changes: readonly DataGridChange[]) => void
  readonly allocation: number
  readonly onAllocationChange: (value: number) => void
  readonly accounts: AccountMaps
  readonly vendorOptions: readonly LookupOption[]
}): ReactElement {
  const { t } = useI18n()
  const columns = buildLandedCostColumns(t, accounts, vendorOptions)
  return (
    <div className="flex flex-col gap-2 border-t border-border-default px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-3.5">
        <h3 className="text-sm font-semibold text-primary">{t('purchase.cost.title')}</h3>
        <div className="min-w-[320px]">
          <Seg
            label={t('purchase.cost.allocation')}
            value={String(allocation)}
            onChange={(value) => {
              onAllocationChange(Number.parseInt(value, 10))
            }}
            options={[
              { value: String(ALLOCATION_BY_VALUE), label: t('purchase.cost.allocation.byValue') },
              { value: String(ALLOCATION_BY_QUANTITY), label: t('purchase.cost.allocation.byQuantity') },
              { value: String(ALLOCATION_MANUAL), label: t('purchase.cost.allocation.manual') },
            ]}
          />
        </div>
        <p className="text-xs text-secondary">{t('purchase.cost.note')}</p>
      </div>
      <DataGrid
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption={t('purchase.cost.caption')}
        cellLabel={(header, rowNumber) =>
          t('purchase.cost.cellLabel', { header, row: String(rowNumber) })
        }
        onCommit={onCommit}
        height={COST_GRID_HEIGHT}
      />
    </div>
  )
}
