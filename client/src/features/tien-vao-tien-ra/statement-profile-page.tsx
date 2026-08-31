/**
 * Màn khai **hồ sơ định dạng sao kê** (RT-26, lát 6G-2) — nợ treo từ 6F-2.
 *
 * Trước màn này, "ngân hàng nào, cột nào" chỉ khai được bằng cách gieo tay một
 * dòng vào DB, nên đường nhập sao kê của 6D thực tế không chạy được ở nhà
 * khách hàng. Không đi đường danh mục tự-sinh (registry nhóm 07):
 * `bank_statement_profiles` là bảng cấu hình cách đọc TỆP, không phải danh mục
 * cây có mã/tên/cha — ép nó vào khuôn ấy là thêm ba cột không nghĩa lý.
 *
 * Hai luật đáng nhớ khi sửa tệp này, cả hai đều là luật của BẢNG (`CHECK` từ
 * 3C-2), không phải của form:
 *
 * * Hình dạng cột tiền là MỘT TRONG HAI: hoặc (Ghi nợ, Ghi có), hoặc (Số tiền
 *   + quy tắc dấu). Khai lẫn cả hai là 409.
 * * Ba ký tự — dấu thập phân, dấu phần nghìn, dấu ngăn cột — phải khác nhau
 *   đôi một. Trùng nhau thì mỗi con số hỏng **im lặng** thành một con số hợp
 *   lệ khác, và đó là lý do màn này tồn tại thay vì một ô nhập tự do.
 *
 * Form vì thế không tự kiểm hai luật ấy: nó gửi lên và hiển thị 409 của server
 * — một bản kiểm thứ hai ở client là một bản có thể trôi khỏi bản thật.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { DataTableColumn } from '@/design-system/components'
import { Alert, Button, DataTable, Drawer, TextField } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import {
  useAllStatementProfiles,
  useDeleteStatementProfile,
  useSaveStatementProfile,
  type StatementProfileDetail,
  type StatementProfileInput,
} from './use-bank-statements'

/** Ô nhập của form, dạng chuỗi — chuyển sang thân API lúc Cất.
 *
 * `-?` bỏ tính tùy chọn của thân API: một ô người dùng nhìn thấy luôn có giá
 * trị (chuỗi rỗng là "chưa nhập"), còn "vắng mặt" là khái niệm của thân JSON
 * chứ không của form. Thiếu `-?` thì `draft[key]` mang thêm `undefined` và mọi
 * ô phải tự đoán mặc định. */
type ProfileDraft = {
  readonly [K in keyof StatementProfileInput]-?: string
}

type SignRule = Exclude<StatementProfileInput['sign_rule'], undefined>

const EMPTY_DRAFT: ProfileDraft = {
  bank_id: '',
  name: '',
  file_kind: 'csv',
  header_row: '1',
  date_col: '',
  date_format: '%d/%m/%Y',
  debit_col: '',
  credit_col: '',
  amount_col: '',
  sign_rule: '',
  ref_col: '',
  description_col: '',
  balance_col: '',
  decimal_sep: '.',
  thousand_sep: '',
  csv_delimiter: ';',
}

/** Ô trống = "tệp không có cột này" → `null`, khác hẳn chuỗi rỗng (xem
 * docstring `thousand_sep` phía server: chuỗi rỗng lọt qua mọi `CHECK`). */
function orNull(value: string): string | null {
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

function draftOf(profile: StatementProfileDetail): ProfileDraft {
  return {
    bank_id: String(profile.bank_id),
    name: profile.name,
    file_kind: profile.file_kind,
    header_row: String(profile.header_row),
    date_col: profile.date_col,
    date_format: profile.date_format,
    debit_col: profile.debit_col ?? '',
    credit_col: profile.credit_col ?? '',
    amount_col: profile.amount_col ?? '',
    sign_rule: profile.sign_rule ?? '',
    ref_col: profile.ref_col ?? '',
    description_col: profile.description_col ?? '',
    balance_col: profile.balance_col ?? '',
    decimal_sep: profile.decimal_sep,
    thousand_sep: profile.thousand_sep ?? '',
    csv_delimiter: profile.csv_delimiter ?? '',
  }
}

function bodyOf(draft: ProfileDraft): StatementProfileInput {
  return {
    bank_id: Number.parseInt(draft.bank_id, 10),
    name: draft.name.trim(),
    file_kind: draft.file_kind as StatementProfileInput['file_kind'],
    header_row: Number.parseInt(draft.header_row, 10),
    date_col: draft.date_col.trim(),
    date_format: draft.date_format.trim(),
    debit_col: orNull(draft.debit_col),
    credit_col: orNull(draft.credit_col),
    amount_col: orNull(draft.amount_col),
    sign_rule: orNull(draft.sign_rule) as SignRule,
    ref_col: orNull(draft.ref_col),
    description_col: orNull(draft.description_col),
    balance_col: orNull(draft.balance_col),
    decimal_sep: draft.decimal_sep,
    thousand_sep: orNull(draft.thousand_sep),
    csv_delimiter: orNull(draft.csv_delimiter),
  }
}

export function StatementProfilePage(): ReactElement {
  const { t } = useI18n()
  const profiles = useAllStatementProfiles()
  const save = useSaveStatementProfile()
  const remove = useDeleteStatementProfile()

  const [editing, setEditing] = useState<StatementProfileDetail | null>(null)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<ProfileDraft>(EMPTY_DRAFT)

  const field = (key: keyof ProfileDraft, label: string): ReactElement => (
    <TextField
      label={label}
      value={draft[key]}
      onChange={(event) => {
        setDraft((current) => ({ ...current, [key]: event.target.value }))
      }}
    />
  )

  const startCreate = (): void => {
    setEditing(null)
    setDraft(EMPTY_DRAFT)
    save.reset()
    setOpen(true)
  }

  const startEdit = (profile: StatementProfileDetail): void => {
    setEditing(profile)
    setDraft(draftOf(profile))
    save.reset()
    setOpen(true)
  }

  const columns: DataTableColumn<StatementProfileDetail>[] = [
    { key: 'name', header: t('cashflow.profile.column.name'), render: (row) => row.name },
    { key: 'bank_id', header: t('cashflow.profile.column.bank'), render: (row) => row.bank_id },
    {
      key: 'file_kind',
      header: t('cashflow.profile.column.fileKind'),
      render: (row) => row.file_kind,
    },
    {
      key: 'amount_shape',
      header: t('cashflow.profile.column.amountShape'),
      render: (row) =>
        row.amount_col === null
          ? `${row.debit_col ?? ''} / ${row.credit_col ?? ''}`
          : `${row.amount_col} (${row.sign_rule ?? ''})`,
    },
    {
      key: 'actions',
      header: t('cashflow.profile.column.actions'),
      render: (row) => (
        <span className="flex gap-2">
          <Button
            variant="ghost"
            onClick={() => {
              startEdit(row)
            }}
          >
            {t('catalog.action.edit', { code: row.name })}
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              remove.mutate({ profileId: row.id })
            }}
          >
            {t('catalog.drawer.delete')}
          </Button>
        </span>
      ),
    },
  ]

  const errorOf = (error: unknown): string | null =>
    error instanceof ApiError ? translateErrorCode(t, error.errorCode) : null

  const listError = errorOf(profiles.error)
  const removeError = errorOf(remove.error)
  const saveError = errorOf(save.error)

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex items-center justify-between">
          <h1 className="text-lg font-semibold text-primary">{t('cashflow.profile.title')}</h1>
          <Button onClick={startCreate}>{t('cashflow.profile.new')}</Button>
        </header>
        <p className="text-xs text-text-muted">{t('cashflow.profile.hint')}</p>

        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {removeError !== null && <Alert tone="error">{removeError}</Alert>}

        <DataTable
          caption={t('cashflow.profile.title')}
          columns={columns}
          rows={profiles.data?.items ?? []}
          rowKey={(row) => String(row.id)}
          emptyLabel={t('cashflow.profile.empty')}
          loading={profiles.isPending}
          loadingLabel={t('common.loading')}
          zebra
        />

        {/* Gắn `key` theo bản ghi đang sửa và chỉ dựng khi mở: state của form
            phải chết cùng lượt sửa, không sống ngầm sang lượt sau (bài học
            6F-2 L-1 — drawer luôn-gắn giữ lại tệp của lượt đã hủy). */}
        {open && (
          <Drawer
            key={editing?.id ?? 'new'}
            open
            title={
              editing === null ? t('cashflow.profile.new') : t('cashflow.profile.editTitle')
            }
            closeLabel={t('common.close')}
            onClose={() => {
              setOpen(false)
            }}
          >
            <div className="grid grid-cols-2 gap-3">
              {field('bank_id', t('cashflow.profile.field.bank'))}
              {field('name', t('cashflow.profile.field.name'))}
              {field('file_kind', t('cashflow.profile.field.fileKind'))}
              {field('header_row', t('cashflow.profile.field.headerRow'))}
              {field('date_col', t('cashflow.profile.field.dateCol'))}
              {field('date_format', t('cashflow.profile.field.dateFormat'))}
              {field('debit_col', t('cashflow.profile.field.debitCol'))}
              {field('credit_col', t('cashflow.profile.field.creditCol'))}
              {field('amount_col', t('cashflow.profile.field.amountCol'))}
              {field('sign_rule', t('cashflow.profile.field.signRule'))}
              {field('ref_col', t('cashflow.profile.field.refCol'))}
              {field('description_col', t('cashflow.profile.field.descriptionCol'))}
              {field('balance_col', t('cashflow.profile.field.balanceCol'))}
              {field('decimal_sep', t('cashflow.profile.field.decimalSep'))}
              {field('thousand_sep', t('cashflow.profile.field.thousandSep'))}
              {field('csv_delimiter', t('cashflow.profile.field.csvDelimiter'))}
            </div>

            {saveError !== null && (
              <div className="mt-3">
                <Alert tone="error">{saveError}</Alert>
              </div>
            )}

            <footer className="mt-4 flex justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setOpen(false)
                }}
              >
                {t('common.cancel')}
              </Button>
              <Button
                disabled={save.isPending}
                onClick={() => {
                  save.mutate(
                    {
                      profileId: editing?.id ?? null,
                      rowVersion: editing?.row_version ?? null,
                      body: bodyOf(draft),
                    },
                    {
                      onSuccess: () => {
                        setOpen(false)
                      },
                    },
                  )
                }}
              >
                {t('catalog.drawer.save')}
              </Button>
            </footer>
          </Drawer>
        )}
      </section>
    </div>
  )
}
