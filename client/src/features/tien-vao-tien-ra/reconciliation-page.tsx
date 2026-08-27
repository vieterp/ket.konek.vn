/**
 * Màn Đối chiếu ngân hàng — danh sách sao kê (bước 20, lát 6F-2).
 *
 * Chọn TK ngân hàng → danh sách sao kê đã nhập; nhập sao kê mới theo hồ sơ
 * per-bank (RT-26 — server lọc hồ sơ theo ngân hàng CỦA tài khoản); mở một
 * sao kê là sang màn hai khung `/tien-vao-tien-ra/doi-chieu/:id` (U5).
 *
 * Nhập trùng tệp trả 409 `bank_statement.duplicate` — thông điệp bảo xóa sao
 * kê cũ trước, không phải một lỗi mạng để thử lại.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { DataTableColumn, LookupOption } from '@/design-system/components'
import { Alert, Button, DataTable, Drawer, LookupInput, SelectField } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { useLookupOptions } from '@/features/danh-muc-thiet-lap/use-lookup-options'

import { FeatureNav } from './feature-nav'
import {
  useBankStatements,
  useDeleteStatement,
  useImportStatement,
  useStatementProfiles,
  type BankStatement,
} from './use-bank-statements'

export function ReconciliationPage(): ReactElement {
  const { t, locale } = useI18n()
  const { readOnly } = useSession()
  const navigate = useNavigate()

  const [bankAccount, setBankAccount] = useState<LookupOption | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)

  const bankAccounts = useLookupOptions('company_bank_accounts')
  const statements = useBankStatements(bankAccount?.id ?? null)
  const removal = useDeleteStatement()

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : t('error.transport.unreachable'),
    )
  }

  const columns: DataTableColumn<BankStatement>[] = [
    {
      key: 'statement_date',
      header: t('cashflow.recon.column.date'),
      render: (row) => formatDate(row.statement_date, locale),
    },
    {
      key: 'opening_balance',
      header: t('cashflow.recon.column.opening'),
      align: 'right',
      render: (row) =>
        row.opening_balance === null ? '—' : formatMoney(row.opening_balance, locale),
    },
    {
      key: 'closing_balance',
      header: t('cashflow.recon.column.closing'),
      align: 'right',
      render: (row) =>
        row.closing_balance === null ? '—' : formatMoney(row.closing_balance, locale),
    },
    {
      key: 'imported_at',
      header: t('cashflow.recon.column.importedAt'),
      render: (row) => formatDate(row.imported_at, locale),
    },
    {
      key: 'actions',
      header: t('cashflow.recon.column.actions'),
      render: (row) => (
        <span className="flex flex-wrap gap-2">
          <Button
            variant="ghost"
            onClick={() => {
              void navigate(`/tien-vao-tien-ra/doi-chieu/${row.id}`)
            }}
          >
            {t('cashflow.recon.open')}
          </Button>
          {!readOnly &&
            (pendingDeleteId === row.id ? (
              <>
                <Button
                  variant="secondary"
                  disabled={removal.isPending}
                  onClick={() => {
                    setError(null)
                    removal.mutate(
                      { statementId: row.id },
                      {
                        onSettled: () => {
                          setPendingDeleteId(null)
                        },
                        onError: fail,
                      },
                    )
                  }}
                >
                  {t('cashflow.recon.deleteConfirm')}
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => {
                    setPendingDeleteId(null)
                  }}
                >
                  {t('common.cancel')}
                </Button>
              </>
            ) : (
              <Button
                variant="ghost"
                onClick={() => {
                  setPendingDeleteId(row.id)
                }}
              >
                {t('cashflow.recon.delete')}
              </Button>
            ))}
        </span>
      ),
    },
  ]

  const listError =
    statements.error instanceof ApiError
      ? translateErrorCode(t, statements.error.errorCode)
      : statements.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-end justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t('cashflow.recon.title')}</h1>
          {!readOnly && bankAccount !== null && (
            <Button
              onClick={() => {
                setDrawerOpen(true)
              }}
            >
              {t('cashflow.recon.import')}
            </Button>
          )}
        </header>

        <div className="max-w-md">
          <LookupInput
            label={t('cashflow.recon.bankAccount')}
            value={bankAccount}
            onChange={setBankAccount}
            options={bankAccounts.data ?? []}
            clearLabel={t('catalog.lookup.clear')}
            emptyLabel={t('cashflow.form.lookupEmpty')}
          />
        </div>

        {error !== null && <Alert tone="error">{error}</Alert>}
        {listError !== null && <Alert tone="error">{listError}</Alert>}

        {bankAccount === null ? (
          <p className="text-app text-text-muted">{t('cashflow.recon.pickAccountHint')}</p>
        ) : (
          <DataTable
            caption={t('cashflow.recon.title')}
            columns={columns}
            rows={statements.data?.items ?? []}
            rowKey={(row) => row.id}
            emptyLabel={t('cashflow.recon.empty')}
            loading={statements.isPending}
            loadingLabel={t('common.loading')}
            zebra
          />
        )}

        {bankAccount !== null && (
          <ImportStatementDrawer
            open={drawerOpen}
            onClose={() => {
              setDrawerOpen(false)
            }}
            bankAccount={bankAccount}
          />
        )}
      </section>
    </div>
  )
}

function ImportStatementDrawer({
  open,
  onClose,
  bankAccount,
}: {
  readonly open: boolean
  readonly onClose: () => void
  readonly bankAccount: LookupOption
}): ReactElement | null {
  const { t } = useI18n()
  const [profileId, setProfileId] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [error, setError] = useState<string | null>(null)

  const profiles = useStatementProfiles(open ? bankAccount.id : null)
  const importer = useImportStatement()

  if (!open) {
    return null
  }

  const profileItems = profiles.data?.items ?? []

  function handleImport(): void {
    setError(null)
    if (profileId === '') {
      setError(t('cashflow.recon.error.profileRequired'))
      return
    }
    if (file === null) {
      setError(t('cashflow.recon.error.fileRequired'))
      return
    }
    importer.mutate(
      { bankAccountId: bankAccount.id, profileId: Number.parseInt(profileId, 10), file },
      {
        onSuccess: () => {
          setFile(null)
          onClose()
        },
        onError: (caught) => {
          setError(
            caught instanceof ApiError
              ? translateErrorCode(t, caught.errorCode)
              : t('error.transport.unreachable'),
          )
        },
      },
    )
  }

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title={t('cashflow.recon.import')}
      closeLabel={t('common.close')}
    >
      <div className="flex flex-col gap-3">
        {error !== null && <Alert tone="error">{error}</Alert>}
        <p className="text-app text-text-muted">
          {t('cashflow.recon.importFor', { account: `${bankAccount.code} — ${bankAccount.label}` })}
        </p>
        {profileItems.length === 0 && !profiles.isPending ? (
          <Alert tone="warning">{t('cashflow.recon.noProfiles')}</Alert>
        ) : (
          <SelectField
            label={t('cashflow.recon.profile')}
            value={profileId}
            onChange={(event) => {
              setProfileId(event.target.value)
            }}
            options={[
              { value: '', label: t('cashflow.recon.profilePlaceholder') },
              ...profileItems.map((profile) => ({
                value: String(profile.id),
                label: profile.name,
              })),
            ]}
          />
        )}
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">{t('cashflow.recon.file')}</span>
          <input
            type="file"
            accept=".csv,.xlsx,.xls"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null)
            }}
          />
        </label>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button disabled={importer.isPending} onClick={handleImport}>
            {importer.isPending ? t('cashflow.recon.importing') : t('cashflow.recon.importGo')}
          </Button>
        </div>
      </div>
    </Drawer>
  )
}
