/**
 * Màn đối chiếu hai khung của MỘT sao kê (bước 20, U5): sao kê bên TRÁI ↔ sổ
 * kế toán bên PHẢI, dòng đã khớp mờ đi, chọn một dòng chưa khớp thì khung phải
 * hiện chứng từ gợi ý ghép (số tiền bằng tuyệt đối, ngày ±3 — luật ở server,
 * client chỉ hiển thị danh sách candidates).
 *
 * Khung phải khi KHÔNG chọn dòng nào = báo cáo lệch hai phía tính đến ngày sao
 * kê (`/bank/reconciliation`): chứng từ đã ghi sổ chưa thấy trên sao kê nào.
 * LƯU Ý phạm vi chi nhánh (review 6F-2, M-1): REST API này KHÔNG mang nhãn
 * ngoài-phạm-vi — dòng khớp với chứng từ ngoài chi nhánh của người xem chỉ
 * hiện "Đã khớp" thường, và `unmatched_vouchers` bị RLS thu hẹp âm thầm.
 * Nhãn 6E-1 H-1 sống ở BÁO CÁO metadata `doi-chieu-ngan-hang`, không ở đây;
 * muốn nhãn trên màn này thì phải thêm trường thật vào API (việc 6G nếu cần).
 */

import type { ReactElement, ReactNode } from 'react'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { Alert, Button, SplitPane } from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { translateErrorCode, useI18n, type Locale } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import {
  useAutoMatch,
  useBankStatementDetail,
  useMatchCandidates,
  useMatchLine,
  useReconciliation,
  useUnmatchLine,
  type BankStatementLine,
  type MatchCandidate,
} from './use-bank-statements'

export function ReconciliationDetailPage(): ReactElement {
  const { id } = useParams<{ id: string }>()
  const { t, locale } = useI18n()
  const { readOnly } = useSession()
  const navigate = useNavigate()

  const [selectedLineId, setSelectedLineId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [autoMatchSummary, setAutoMatchSummary] = useState<string | null>(null)

  const detail = useBankStatementDetail(id ?? null)
  const statement = detail.data?.statement ?? null
  const reconciliation = useReconciliation(
    statement?.bank_account_id ?? null,
    statement?.statement_date ?? '',
  )
  const candidates = useMatchCandidates(selectedLineId)
  const autoMatch = useAutoMatch()
  const matcher = useMatchLine()
  const unmatcher = useUnmatchLine()

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : t('error.transport.unreachable'),
    )
  }

  if (detail.isPending) {
    return (
      <Shell title={t('cashflow.recon.detailTitle')}>
        <p className="text-app text-text-muted">{t('common.loading')}</p>
      </Shell>
    )
  }
  if (detail.isError || statement === null) {
    const message =
      detail.error instanceof ApiError
        ? translateErrorCode(t, detail.error.errorCode)
        : t('error.transport.unreachable')
    return (
      <Shell title={t('cashflow.recon.detailTitle')}>
        <Alert tone="error">{message}</Alert>
      </Shell>
    )
  }

  const lines = detail.data?.lines ?? []
  const selectedLine = lines.find((line) => line.id === selectedLineId) ?? null
  const matchedCount = lines.filter((line) => line.matched_voucher_id !== null).length
  const busy = matcher.isPending || unmatcher.isPending || autoMatch.isPending

  const leftPane = (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-2">
      <h2 className="text-sm font-semibold text-primary">
        {t('cashflow.recon.statementSide', {
          date: formatDate(statement.statement_date, locale),
          matched: String(matchedCount),
          total: String(lines.length),
        })}
      </h2>
      <ul className="flex flex-col gap-1">
        {lines.map((line) => (
          <StatementLineRow
            key={line.id}
            line={line}
            locale={locale}
            selected={line.id === selectedLineId}
            onSelect={() => {
              setSelectedLineId((current) => (current === line.id ? null : line.id))
            }}
          />
        ))}
        {lines.length === 0 && (
          <li className="text-app text-text-muted">{t('cashflow.recon.noLines')}</li>
        )}
      </ul>
    </div>
  )

  const rightPane = (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-2">
      {selectedLine === null ? (
        <UnmatchedVouchersPanel
          reconciliation={reconciliation}
          locale={locale}
        />
      ) : selectedLine.matched_voucher_id !== null ? (
        <div className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold text-primary">
            {t('cashflow.recon.matchedLine')}
          </h2>
          {!readOnly && (
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => {
                setError(null)
                unmatcher.mutate(
                  { lineId: selectedLine.id },
                  {
                    onSuccess: () => {
                      setSelectedLineId(null)
                    },
                    onError: fail,
                  },
                )
              }}
            >
              {t('cashflow.recon.unmatch')}
            </Button>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold text-primary">
            {t('cashflow.recon.candidatesFor', {
              amount: formatMoney(
                Number.parseFloat(selectedLine.credit) > 0
                  ? selectedLine.credit
                  : selectedLine.debit,
                locale,
              ),
            })}
          </h2>
          {candidates.isPending && (
            <p className="text-app text-text-muted">{t('common.loading')}</p>
          )}
          <ul className="flex flex-col gap-1">
            {(candidates.data?.items ?? []).map((candidate) => (
              <CandidateRow
                key={candidate.voucher_id}
                candidate={candidate}
                locale={locale}
                disabled={readOnly || busy}
                onMatch={() => {
                  setError(null)
                  matcher.mutate(
                    { lineId: selectedLine.id, voucherId: candidate.voucher_id },
                    {
                      onSuccess: () => {
                        setSelectedLineId(null)
                      },
                      onError: fail,
                    },
                  )
                }}
              />
            ))}
            {!candidates.isPending && (candidates.data?.items ?? []).length === 0 && (
              <li className="text-app text-text-muted">{t('cashflow.recon.noCandidates')}</li>
            )}
          </ul>
        </div>
      )}
    </div>
  )

  return (
    <Shell title={t('cashflow.recon.detailTitle')}>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="ghost"
          onClick={() => {
            void navigate('/tien-vao-tien-ra/doi-chieu')
          }}
        >
          {t('cashflow.recon.backToList')}
        </Button>
        {!readOnly && (
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => {
              setError(null)
              setAutoMatchSummary(null)
              autoMatch.mutate(
                { statementId: statement.id },
                {
                  onSuccess: (result) => {
                    setAutoMatchSummary(
                      t('cashflow.recon.autoMatchDone', {
                        matched: String(result.matched),
                        unmatched: String(result.unmatched_lines),
                        ambiguous: String(result.ambiguous_lines),
                      }),
                    )
                  },
                  onError: fail,
                },
              )
            }}
          >
            {t('cashflow.recon.autoMatch')}
          </Button>
        )}
      </div>

      {error !== null && <Alert tone="error">{error}</Alert>}
      {autoMatchSummary !== null && <Alert tone="info">{autoMatchSummary}</Alert>}

      <div className="min-h-0 flex-1">
        <SplitPane
          left={leftPane}
          right={rightPane}
          separatorLabel={t('cashflow.recon.separator')}
          storageKey="doi-chieu-ngan-hang"
        />
      </div>
    </Shell>
  )
}

function Shell({
  title,
  children,
}: {
  readonly title: string
  readonly children: ReactNode
}): ReactElement {
  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <h1 className="text-lg font-semibold text-primary">{title}</h1>
        {children}
      </section>
    </div>
  )
}

/** Một dòng sao kê — ĐÃ khớp thì mờ đi (U5), vẫn chọn được để gỡ khớp. */
function StatementLineRow({
  line,
  locale,
  selected,
  onSelect,
}: {
  readonly line: BankStatementLine
  readonly locale: Locale
  readonly selected: boolean
  readonly onSelect: () => void
}): ReactElement {
  const { t } = useI18n()
  const matched = line.matched_voucher_id !== null
  // Parse thay vì so chuỗi '0'/'0.00' — scale serialize phía server đổi thì
  // phép so chuỗi gãy im lặng (bài học L-1 review 6F-1).
  const isCredit = Number.parseFloat(line.credit) > 0
  return (
    <li>
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={`w-full rounded border px-2 py-1 text-left text-sm transition-colors ${
          selected ? 'border-primary bg-navy-50' : 'border-border-default bg-background'
        } ${matched ? 'opacity-50' : ''}`}
      >
        <span className="flex items-center justify-between gap-2">
          <span className="min-w-0 truncate">
            {formatDate(line.txn_date, locale)}
            {line.reference_no !== null && line.reference_no !== '' ? ` · ${line.reference_no}` : ''}
            {line.description !== null && line.description !== '' ? ` · ${line.description}` : ''}
          </span>
          <span className={`shrink-0 font-medium ${isCredit ? 'text-green-700' : 'text-red-700'}`}>
            {isCredit
              ? formatMoney(line.credit, locale)
              : `-${formatMoney(line.debit, locale)}`}
          </span>
        </span>
        {matched && (
          <span className="text-xs text-text-muted">{t('cashflow.recon.lineMatched')}</span>
        )}
      </button>
    </li>
  )
}

function CandidateRow({
  candidate,
  locale,
  disabled,
  onMatch,
}: {
  readonly candidate: MatchCandidate
  readonly locale: Locale
  readonly disabled: boolean
  readonly onMatch: () => void
}): ReactElement {
  const { t } = useI18n()
  return (
    <li className="flex items-center justify-between gap-2 rounded border border-border-default px-2 py-1 text-sm">
      <span className="min-w-0 truncate">
        {candidate.voucher_no} · {formatDate(candidate.posting_date, locale)}
        {candidate.description !== null && candidate.description !== ''
          ? ` · ${candidate.description}`
          : ''}
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <span className="font-medium">{formatMoney(candidate.net_fc, locale)}</span>
        {!disabled && (
          <Button variant="secondary" onClick={onMatch}>
            {t('cashflow.recon.match')}
          </Button>
        )}
      </span>
    </li>
  )
}

/** Khung phải mặc định: phía SỔ còn lệch — chứng từ đã ghi sổ chưa khớp dòng sao kê nào. */
function UnmatchedVouchersPanel({
  reconciliation,
  locale,
}: {
  readonly reconciliation: ReturnType<typeof useReconciliation>
  readonly locale: Locale
}): ReactElement {
  const { t } = useI18n()
  if (reconciliation.isPending) {
    return <p className="text-app text-text-muted">{t('common.loading')}</p>
  }
  if (reconciliation.isError) {
    const message =
      reconciliation.error instanceof ApiError
        ? translateErrorCode(t, reconciliation.error.errorCode)
        : t('error.transport.unreachable')
    return <Alert tone="error">{message}</Alert>
  }
  const vouchers = reconciliation.data?.unmatched_vouchers ?? []
  return (
    <div className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold text-primary">{t('cashflow.recon.ledgerSide')}</h2>
      <p className="text-xs text-text-muted">{t('cashflow.recon.ledgerSideHint')}</p>
      <ul className="flex flex-col gap-1">
        {vouchers.map((candidate) => (
          <li
            key={candidate.voucher_id}
            className="flex items-center justify-between gap-2 rounded border border-border-default px-2 py-1 text-sm"
          >
            <span className="min-w-0 truncate">
              {candidate.voucher_no} · {formatDate(candidate.posting_date, locale)}
              {candidate.description !== null && candidate.description !== ''
                ? ` · ${candidate.description}`
                : ''}
            </span>
            <span className="shrink-0 font-medium">{formatMoney(candidate.net_fc, locale)}</span>
          </li>
        ))}
        {vouchers.length === 0 && (
          <li className="text-app text-text-muted">{t('cashflow.recon.ledgerClean')}</li>
        )}
      </ul>
    </div>
  )
}
