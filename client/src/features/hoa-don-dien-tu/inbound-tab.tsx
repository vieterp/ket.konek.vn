/**
 * Tab "Hóa đơn đầu vào" trên trang Hóa đơn điện tử (7F-2b / FR-EIV-040; design
 * nhóm 02 không vẽ màn này — quyết định user 2026-09-18: tab thứ hai cùng
 * trang). Nạp tệp XML chuẩn TCT của người bán, lưới tờ đã nạp với tab con
 * "chưa lập chứng từ" (`pending_only`), drawer xem dòng + tải XML, và hộp
 * "Lập chứng từ mua".
 *
 * Hộp lập chứng từ chỉ hỏi PHẦN KẾ TOÁN (`InboundPurchaseIn`): loại, nghiệp
 * vụ, ba tài khoản, đối tác, ngày hạch toán, điều khoản — mọi con số lấy từ
 * tờ hóa đơn đã nạp, không nhận lại từ client. Đối tác điền sẵn dòng khớp mã
 * số thuế người bán (`vendor_id`); trống thì bắt buộc chọn. Chứng từ lập xong
 * mở ở form mua 7H-1 để sửa từng dòng.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import type { DataTableColumn, LookupOption, TabItem } from '@/design-system/components'
import {
  Alert,
  Button,
  DataTable,
  Drawer,
  LookupInput,
  SelectField,
  StatusPill,
  Tabs,
  TextField,
} from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { saveBlob } from '@/lib/job-tracking'
import { ApiError, useSession } from '@/lib/session'

import { useMasterSearchLookup } from '@/features/danh-muc-thiet-lap/use-master-search-lookup'
import {
  PURCHASE_KIND_ASSET,
  PURCHASE_KIND_GOODS,
  PURCHASE_KIND_IN_TRANSIT,
  PURCHASE_KIND_LABEL_KEYS,
  PURCHASE_KIND_SERVICE,
} from '@/features/mua-hang/purchase-row-status'
import { useAccountLookup } from '@/features/so-sach-thue/use-account-lookup'
import { useAutoPostingOperations } from '@/features/tien-vao-tien-ra/use-auto-posting'

import {
  INBOUND_PAGE_SIZE,
  inboundXmlPath,
  useInboundActions,
  useInboundInvoice,
  useInboundList,
  type InboundEInvoiceOut,
} from './use-inbound-einvoices'

const PURCHASE_DOCUMENT_TYPE = 'PUR'
/** Bốn loại `InboundPurchaseIn.kind` nhận (0–3) — trả lại hàng mua bị loại ở server. */
const INBOUND_KINDS: readonly number[] = [
  PURCHASE_KIND_GOODS,
  PURCHASE_KIND_SERVICE,
  PURCHASE_KIND_ASSET,
  PURCHASE_KIND_IN_TRANSIT,
]
/** Mã lỗi server khi tờ hóa đơn đã có trong sổ (`InboundEInvoiceDuplicateError`). */
const DUPLICATE_ERROR_CODE = 'einvoice.inbound_duplicate'

function optionOf(id: number, code: string, label: string): LookupOption {
  return { id, code, label }
}

function ImportBox({ onImported }: { readonly onImported: (row: InboundEInvoiceOut) => void }): ReactElement {
  const { t } = useI18n()
  const actions = useInboundActions()
  const [file, setFile] = useState<File | null>(null)
  const [key, setKey] = useState(newIdempotencyKey)
  const [message, setMessage] = useState<{ tone: 'error' | 'info'; text: string } | null>(null)

  function submit(): void {
    if (file === null) {
      return
    }
    setMessage(null)
    actions.importXml.mutate(
      { file, idempotencyKey: key },
      {
        onSuccess: (row) => {
          setFile(null)
          setKey(newIdempotencyKey())
          onImported(row)
        },
        onError: (caught) => {
          if (caught instanceof ApiError && caught.errorCode === DUPLICATE_ERROR_CODE) {
            setMessage({ tone: 'info', text: t('einvoice.inbound.alreadyImported') })
            return
          }
          setMessage({
            tone: 'error',
            text:
              caught instanceof ApiError
                ? translateErrorCode(t, caught.errorCode)
                : t('error.transport.unreachable'),
          })
        },
      },
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded border border-border-default p-3">
      <p className="text-sm font-semibold text-primary">{t('einvoice.inbound.importTitle')}</p>
      <p className="text-xs text-text-muted">{t('einvoice.inbound.importHint')}</p>
      {message !== null && <Alert tone={message.tone}>{message.text}</Alert>}
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-sm">
          {t('einvoice.inbound.file')}
          <input
            type="file"
            accept=".xml,application/xml,text/xml"
            className="ml-2 text-sm"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null)
              // Khóa mới cho mỗi lần chọn tệp: hai tệp khác nhau là hai lượt nạp.
              setKey(newIdempotencyKey())
            }}
          />
        </label>
        <Button onClick={submit} disabled={file === null || actions.importXml.isPending}>
          {t('einvoice.inbound.importSubmit')}
        </Button>
      </div>
    </div>
  )
}

function CreatePurchaseDrawer({
  invoice,
  onClose,
  onCreated,
}: {
  readonly invoice: InboundEInvoiceOut | null
  readonly onClose: () => void
  readonly onCreated: (voucherId: string) => void
}): ReactElement {
  const { t } = useI18n()
  const actions = useInboundActions()
  const postingDefault = invoice?.invoice_date ?? ''
  const [kind, setKind] = useState(String(PURCHASE_KIND_SERVICE))
  const [operationCode, setOperationCode] = useState('')
  const [postingDate, setPostingDate] = useState(postingDefault)
  const [payable, setPayable] = useState<LookupOption | null>(null)
  const [debit, setDebit] = useState<LookupOption | null>(null)
  const [vat, setVat] = useState<LookupOption | null>(null)
  const [vendor, setVendor] = useState<LookupOption | null>(null)
  const [paymentTerm, setPaymentTerm] = useState<LookupOption | null>(null)
  const [description, setDescription] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [key] = useState(newIdempotencyKey)

  const effectiveDate = postingDate === '' ? postingDefault : postingDate
  const operations = useAutoPostingOperations(PURCHASE_DOCUMENT_TYPE, effectiveDate)
  const accounts = useAccountLookup(effectiveDate)
  const vendors = useMasterSearchLookup('partners', invoice?.vendor_id === null || invoice?.vendor_id === undefined ? [] : [invoice.vendor_id])
  const paymentTerms = useMasterSearchLookup('payment_terms')

  const accountOptions: LookupOption[] = [...accounts.maps.byCode.values()]
    .filter((account) => !account.is_summary)
    .map((account) => optionOf(account.id, account.code, `${account.code} · ${account.name}`))
  const payableOptions = [...accounts.maps.byCode.values()]
    .filter((account) => !account.is_summary && (account.detail_tracking ?? []).includes('vendor'))
    .map((account) => optionOf(account.id, account.code, `${account.code} · ${account.name}`))
  const matchedVendor =
    invoice?.vendor_id === null || invoice?.vendor_id === undefined
      ? null
      : (vendors.byId.get(invoice.vendor_id) ?? null)
  const vendorValue = vendor ?? matchedVendor

  function submit(): void {
    if (invoice === null) {
      return
    }
    if (operationCode === '' || payable === null || debit === null) {
      setError(t('einvoice.inbound.createRequired'))
      return
    }
    if (vendorValue === null) {
      setError(t('einvoice.inbound.vendorRequired'))
      return
    }
    setError(null)
    actions.createPurchase.mutate(
      {
        id: invoice.id,
        idempotencyKey: key,
        body: {
          kind: Number.parseInt(kind, 10),
          operation_code: operationCode,
          payable_account_id: payable.id,
          account_id: debit.id,
          vat_account_id: vat?.id ?? null,
          vendor_id: vendorValue.id,
          posting_date: effectiveDate === '' ? null : effectiveDate,
          payment_term_id: paymentTerm?.id ?? null,
          description: description.trim() === '' ? null : description.trim(),
        },
      },
      {
        onSuccess: (row) => {
          if (row.voucher_id != null) {
            onCreated(row.voucher_id)
          }
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
      open={invoice !== null}
      title={
        invoice === null
          ? ''
          : t('einvoice.inbound.createTitle', { no: `${invoice.invoice_serial} ${invoice.invoice_no}` })
      }
      onClose={onClose}
      closeLabel={t('common.close')}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} disabled={actions.createPurchase.isPending}>
            {t('einvoice.inbound.createSubmit')}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        {error !== null && <Alert tone="error">{error}</Alert>}
        <p className="text-xs text-text-muted">{t('einvoice.inbound.createHint')}</p>
        <SelectField
          label={t('einvoice.inbound.kind')}
          value={kind}
          onChange={(event) => {
            setKind(event.target.value)
          }}
          options={INBOUND_KINDS.map((value) => ({
            value: String(value),
            label: t(PURCHASE_KIND_LABEL_KEYS[value as keyof typeof PURCHASE_KIND_LABEL_KEYS]),
          }))}
        />
        <TextField
          label={t('einvoice.inbound.postingDate')}
          type="date"
          value={effectiveDate}
          onChange={(event) => {
            setPostingDate(event.target.value)
          }}
        />
        <SelectField
          label={t('purchase.form.operation')}
          value={operationCode}
          onChange={(event) => {
            setOperationCode(event.target.value)
          }}
          options={[
            { value: '', label: t('purchase.form.operationPlaceholder') },
            ...(operations.data?.items ?? []).map((operation) => ({
              value: operation.operation_code,
              label: operation.operation_name,
            })),
          ]}
        />
        <LookupInput
          label={t('einvoice.inbound.vendor')}
          value={vendorValue}
          onChange={setVendor}
          options={vendors.options}
          onQueryChange={vendors.searchFor}
          clearLabel={t('common.close')}
          emptyLabel={t('einvoice.inbound.noMatch')}
          hint={matchedVendor === null ? t('einvoice.inbound.vendorUnmatched', { taxCode: invoice?.seller_tax_code ?? '' }) : undefined}
        />
        <LookupInput
          label={t('purchase.form.payableAccount')}
          value={payable}
          onChange={setPayable}
          options={payableOptions}
          clearLabel={t('common.close')}
          emptyLabel={t('einvoice.inbound.noMatch')}
        />
        <LookupInput
          label={t('einvoice.inbound.debitAccount')}
          value={debit}
          onChange={setDebit}
          options={accountOptions}
          clearLabel={t('common.close')}
          emptyLabel={t('einvoice.inbound.noMatch')}
        />
        <LookupInput
          label={t('einvoice.inbound.vatAccount')}
          value={vat}
          onChange={setVat}
          options={accountOptions}
          clearLabel={t('common.close')}
          emptyLabel={t('einvoice.inbound.noMatch')}
        />
        <LookupInput
          label={t('einvoice.inbound.paymentTerm')}
          value={paymentTerm}
          onChange={setPaymentTerm}
          options={paymentTerms.options}
          clearLabel={t('common.close')}
          emptyLabel={t('einvoice.inbound.noMatch')}
        />
        <TextField
          label={t('einvoice.inbound.description')}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value)
          }}
        />
      </div>
    </Drawer>
  )
}

function LinesDrawer({
  id,
  onClose,
}: {
  readonly id: string | null
  readonly onClose: () => void
}): ReactElement {
  const { t, locale } = useI18n()
  const { client, datasetCode } = useSession()
  const detail = useInboundInvoice(id)
  const [error, setError] = useState<string | null>(null)
  const invoice = detail.data ?? null

  async function downloadXml(): Promise<void> {
    if (invoice === null) {
      return
    }
    setError(null)
    try {
      const file = await client.getBlob(inboundXmlPath(invoice.id), { datasetCode })
      saveBlob(file.blob, file.fileName ?? invoice.file_name)
    } catch (caught) {
      setError(
        caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : t('error.transport.unreachable'),
      )
    }
  }

  return (
    <Drawer
      open={id !== null}
      title={invoice === null ? t('common.loading') : `${invoice.invoice_serial} ${invoice.invoice_no}`}
      onClose={onClose}
      closeLabel={t('common.close')}
      footer={
        <div className="flex justify-end">
          <Button
            variant="secondary"
            onClick={() => {
              void downloadXml()
            }}
            disabled={invoice === null}
          >
            {t('einvoice.inbound.downloadXml')}
          </Button>
        </div>
      }
    >
      {error !== null && <Alert tone="error">{error}</Alert>}
      {invoice !== null && (
        <div className="flex flex-col gap-3 text-sm">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-text-muted">{t('einvoice.inbound.column.seller')}</dt>
            <dd>
              {invoice.seller_name} · {invoice.seller_tax_code}
            </dd>
            <dt className="text-text-muted">{t('einvoice.list.column.date')}</dt>
            <dd>{formatDate(invoice.invoice_date, locale)}</dd>
            <dt className="text-text-muted">{t('einvoice.list.column.total')}</dt>
            <dd className="tabular-nums">
              {formatMoney(invoice.total_amount, locale)} {invoice.currency_code}
            </dd>
          </dl>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-text-muted">
                <th>{t('einvoice.inbound.line.description')}</th>
                <th className="text-right">{t('einvoice.inbound.line.quantity')}</th>
                <th className="text-right">{t('einvoice.inbound.line.amount')}</th>
                <th className="text-right">{t('einvoice.inbound.line.vat')}</th>
              </tr>
            </thead>
            <tbody>
              {invoice.lines.map((line) => (
                <tr key={line.line_no}>
                  <td>{line.description}</td>
                  <td className="text-right tabular-nums">
                    {line.quantity === null ? '' : `${line.quantity} ${line.unit ?? ''}`}
                  </td>
                  <td className="text-right tabular-nums">{formatMoney(line.amount, locale)}</td>
                  <td className="text-right tabular-nums">
                    {line.vat_rate_text ?? line.vat_rate ?? ''} · {formatMoney(line.vat_amount, locale)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Drawer>
  )
}

export function InboundTab(): ReactElement {
  const { t, locale } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()
  const actions = useInboundActions()
  const [tab, setTab] = useState('pending')
  const [page, setPage] = useState(1)
  const [viewing, setViewing] = useState<string | null>(null)
  const [creating, setCreating] = useState<InboundEInvoiceOut | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const list = useInboundList(tab === 'pending', page)
  const rows = list.data?.items ?? []
  const tabs: TabItem[] = [
    { id: 'pending', label: t('einvoice.inbound.tabPending'), ...(list.data === undefined ? {} : { count: list.data.pending }) },
    { id: 'all', label: t('einvoice.inbound.tabAll') },
  ]

  const columns: DataTableColumn<InboundEInvoiceOut>[] = [
    {
      key: 'invoice_date',
      header: t('einvoice.list.column.date'),
      render: (row) => formatDate(row.invoice_date, locale),
    },
    {
      key: 'invoice_no',
      header: t('einvoice.list.column.no'),
      render: (row) => (
        <button
          type="button"
          className="text-secondary hover:underline"
          onClick={() => {
            setViewing(row.id)
          }}
        >
          {row.invoice_serial} {row.invoice_no}
        </button>
      ),
    },
    {
      key: 'seller',
      header: t('einvoice.inbound.column.seller'),
      render: (row) => `${row.seller_name} · ${row.seller_tax_code}`,
    },
    {
      key: 'total_amount',
      header: t('einvoice.list.column.total'),
      align: 'right',
      render: (row) => formatMoney(row.total_amount, locale),
    },
    {
      key: 'voucher',
      header: t('einvoice.inbound.column.voucher'),
      render: (row) =>
        row.voucher_id === null ? (
          <StatusPill tone="todo">{t('einvoice.inbound.status.pending')}</StatusPill>
        ) : (
          <button
            type="button"
            className="text-secondary hover:underline"
            onClick={() => {
              void navigate(`/mua-hang/chung-tu/${String(row.voucher_id)}`)
            }}
          >
            {t('einvoice.inbound.status.created')}
          </button>
        ),
    },
    {
      key: 'actions',
      header: t('einvoice.list.column.nextAction'),
      render: (row) =>
        readOnly || row.voucher_id !== null ? (
          '—'
        ) : (
          <span className="flex gap-2 text-xs">
            <Button
              variant="secondary"
              onClick={() => {
                setCreating(row)
              }}
            >
              {t('einvoice.inbound.action.create')}
            </Button>
            {confirmDelete === row.id ? (
              <Button
                variant="ghost"
                onClick={() => {
                  setActionError(null)
                  actions.remove.mutate(row.id, {
                    onSettled: () => {
                      setConfirmDelete(null)
                    },
                    onError: (caught) => {
                      setActionError(
                        caught instanceof ApiError
                          ? translateErrorCode(t, caught.errorCode)
                          : t('error.transport.unreachable'),
                      )
                    },
                  })
                }}
              >
                {t('einvoice.inbound.action.deleteConfirm')}
              </Button>
            ) : (
              <Button
                variant="ghost"
                onClick={() => {
                  setConfirmDelete(row.id)
                }}
              >
                {t('einvoice.inbound.action.delete')}
              </Button>
            )}
          </span>
        ),
    },
  ]

  const listError =
    list.error instanceof ApiError
      ? translateErrorCode(t, list.error.errorCode)
      : list.isError
        ? t('error.transport.unreachable')
        : null

  return (
    <div className="flex flex-col gap-3">
      {!readOnly && (
        <ImportBox
          onImported={(row) => {
            setNotice(t('einvoice.inbound.imported', { no: `${row.invoice_serial} ${row.invoice_no}` }))
          }}
        />
      )}
      {notice !== null && <Alert tone="info">{notice}</Alert>}
      {listError !== null && <Alert tone="error">{listError}</Alert>}
      {actionError !== null && <Alert tone="error">{actionError}</Alert>}
      <Tabs
        label={t('einvoice.inbound.tabsLabel')}
        tabs={tabs}
        activeId={tab}
        onChange={(id) => {
          setTab(id)
          setPage(1)
        }}
      >
        <div className="flex flex-col gap-2">
          <DataTable
            caption={t('einvoice.mode.inbound')}
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            emptyLabel={t('einvoice.inbound.empty')}
            loading={list.isPending}
            loadingLabel={t('common.loading')}
            zebra
          />
          <footer className="flex justify-end gap-2">
            <Button
              variant="ghost"
              disabled={page <= 1}
              onClick={() => {
                setPage((current) => Math.max(1, current - 1))
              }}
            >
              {t('einvoice.list.prev')}
            </Button>
            <Button
              variant="ghost"
              disabled={rows.length < INBOUND_PAGE_SIZE}
              onClick={() => {
                setPage((current) => current + 1)
              }}
            >
              {t('einvoice.list.next')}
            </Button>
          </footer>
        </div>
      </Tabs>
      <LinesDrawer
        id={viewing}
        onClose={() => {
          setViewing(null)
        }}
      />
      {creating !== null && (
        <CreatePurchaseDrawer
          invoice={creating}
          onClose={() => {
            setCreating(null)
          }}
          onCreated={(voucherId) => {
            setCreating(null)
            void navigate(`/mua-hang/chung-tu/${voucherId}`)
          }}
        />
      )}
    </div>
  )
}
