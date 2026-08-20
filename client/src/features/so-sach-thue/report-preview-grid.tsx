/**
 * Lưới xem trước báo cáo (bước 14 phase-05) — client CHỈ VẼ, không tính.
 *
 * Ô đến từ server đã qua đúng pha chữ của bản in (`presentation.py`), kèm lớp
 * CSS trình bày (`cell-right`, `cell-money`…). Ở đây chỉ dịch hai lớp căn lề
 * sang tiện ích Tailwind — con số, dấu phẩy, thụt lề nhóm đều giữ nguyên từ
 * server để lưới và tờ giấy không bao giờ lệch nhau (BR-RPT-02).
 */

import type { ReactElement } from 'react'

import { Alert } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { PreviewRow, ReportPreview } from './use-reports'

function cellClass(css: string): string {
  const align = css.includes('cell-right')
    ? 'text-right'
    : css.includes('cell-center')
      ? 'text-center'
      : 'text-left'
  return `border-b border-border-default px-2 py-1 tabular-nums ${align}`
}

function headerClass(align: string | null): string {
  const alignment = align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : 'text-left'
  return `border-b-2 border-border-default bg-surface px-2 py-1 font-semibold ${alignment}`
}

function rowKey(row: PreviewRow, index: number): string {
  return `${row.kind}-${String(index)}`
}

export function ReportPreviewGrid({ preview }: { readonly preview: ReportPreview }): ReactElement {
  const { t } = useI18n()
  const columnCount = preview.columns.length

  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div>
        <h3 className="text-base font-semibold text-text-default">{preview.name}</h3>
        {preview.param_lines.map((line) => (
          <p key={line} className="text-sm text-text-muted">
            {line}
          </p>
        ))}
      </div>
      {preview.truncated ? <Alert tone="warning">{t('reports.preview.truncated')}</Alert> : null}
      <div className="min-h-0 overflow-auto rounded border border-border-default">
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0">
            <tr>
              {preview.columns.map((column) => (
                <th key={column.key} className={headerClass(column.align)}>
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preview.rows.map((row, index) => {
              if (row.kind === 'group_header') {
                return (
                  <tr key={rowKey(row, index)}>
                    <td
                      colSpan={columnCount}
                      className="border-b border-border-default bg-surface px-2 py-1 font-semibold"
                    >
                      {row.heading}
                    </td>
                  </tr>
                )
              }
              const emphasized = row.kind === 'group_footer' || row.kind === 'grand_total'
              return (
                <tr key={rowKey(row, index)} className={emphasized ? 'font-semibold' : undefined}>
                  {(row.cells ?? []).map((cell, cellIndex) => (
                    <td
                      // Ô không có danh tính riêng — vị trí trong dòng là khóa.
                      key={`${rowKey(row, index)}-${String(cellIndex)}`}
                      className={cellClass(cell.css)}
                      colSpan={
                        emphasized && cellIndex === 0 ? (row.label_span ?? 1) : undefined
                      }
                    >
                      {cell.text}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
