/**
 * Sao kê + đối chiếu ngân hàng (`/api/v1/bank/statements*`, `/bank/reconciliation`
 * — API lát 6D, UI lát 6F-2).
 *
 * Các hành động khớp/gỡ/nhập/xóa KHÔNG gửi khóa idempotency — bốn route này
 * được miễn trừ có chủ đích phía server (nhập chốt bằng khóa băm nội dung,
 * khớp/gỡ là ghi-trạng-thái đâm vào 409 khi đua).
 *
 * Khóa truy vấn dưới tiền tố `['bank-statements', dataset]` — một hành động
 * khớp làm mới cả hai khung lẫn báo cáo lệch bằng một lệnh invalidate.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type BankStatement = Schemas['BankStatementOut']
export type BankStatementLine = Schemas['BankStatementLineOut']
export type BankStatementDetail = Schemas['BankStatementDetailResponse']
export type BankStatementImportResult = Schemas['BankStatementImportOut']
export type StatementProfile = Schemas['BankStatementProfileOut']
export type MatchCandidate = Schemas['MatchCandidateOut']
export type Reconciliation = Schemas['ReconciliationResponse']
export type AutoMatchResult = Schemas['AutoMatchResponse']

export function useBankStatements(bankAccountId: number | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['bank-statements', datasetCode, 'list', bankAccountId],
    enabled: datasetCode !== null && bankAccountId !== null,
    queryFn: () =>
      client.get<Schemas['BankStatementListResponse']>(
        `/api/v1/bank/statements?bank_account_id=${String(bankAccountId)}`,
        { datasetCode },
      ),
  })
}

export function useBankStatementDetail(statementId: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['bank-statements', datasetCode, 'detail', statementId],
    enabled: datasetCode !== null && statementId !== null,
    queryFn: () =>
      client.get<BankStatementDetail>(`/api/v1/bank/statements/${statementId ?? ''}`, {
        datasetCode,
      }),
  })
}

export function useStatementProfiles(bankAccountId: number | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['bank-statements', datasetCode, 'profiles', bankAccountId],
    enabled: datasetCode !== null && bankAccountId !== null,
    queryFn: () =>
      client.get<Schemas['BankStatementProfileListResponse']>(
        `/api/v1/bank/statements/profiles?bank_account_id=${String(bankAccountId)}`,
        { datasetCode },
      ),
  })
}

/** Báo cáo lệch hai phía tính đến hết `asOf` (FR-BNK-031) — nguồn khung PHẢI. */
export function useReconciliation(bankAccountId: number | null, asOf: string) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['bank-statements', datasetCode, 'reconciliation', bankAccountId, asOf],
    enabled: datasetCode !== null && bankAccountId !== null && asOf !== '',
    queryFn: () =>
      client.get<Reconciliation>(
        `/api/v1/bank/reconciliation?bank_account_id=${String(bankAccountId)}&as_of=${asOf}`,
        { datasetCode },
      ),
  })
}

export function useMatchCandidates(lineId: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['bank-statements', datasetCode, 'candidates', lineId],
    enabled: datasetCode !== null && lineId !== null,
    queryFn: () =>
      client.get<Schemas['MatchCandidatesResponse']>(
        `/api/v1/bank/statements/lines/${lineId ?? ''}/candidates`,
        { datasetCode },
      ),
  })
}

function useInvalidateStatements(): () => void {
  const { datasetCode } = useSession()
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['bank-statements', datasetCode] })
  }
}

export function useImportStatement() {
  const { client, datasetCode } = useSession()
  const invalidate = useInvalidateStatements()

  return useMutation({
    mutationFn: ({
      bankAccountId,
      profileId,
      file,
    }: {
      readonly bankAccountId: number
      readonly profileId: number
      readonly file: File
    }) => {
      const form = new FormData()
      form.append('file', file)
      form.append('bank_account_id', String(bankAccountId))
      form.append('profile_id', String(profileId))
      return client.postForm<BankStatementImportResult>('/api/v1/bank/statements/import', form, {
        datasetCode,
      })
    },
    onSuccess: invalidate,
  })
}

export function useDeleteStatement() {
  const { client, datasetCode } = useSession()
  const invalidate = useInvalidateStatements()

  return useMutation({
    mutationFn: ({ statementId }: { readonly statementId: string }) =>
      client.delete<void>(`/api/v1/bank/statements/${statementId}`, { datasetCode }),
    onSuccess: invalidate,
  })
}

export function useAutoMatch() {
  const { client, datasetCode } = useSession()
  const invalidate = useInvalidateStatements()

  return useMutation({
    mutationFn: ({ statementId }: { readonly statementId: string }) =>
      client.post<AutoMatchResult>(
        `/api/v1/bank/statements/${statementId}/actions/auto-match`,
        undefined,
        { datasetCode },
      ),
    onSuccess: invalidate,
  })
}

export function useMatchLine() {
  const { client, datasetCode } = useSession()
  const invalidate = useInvalidateStatements()

  return useMutation({
    mutationFn: ({
      lineId,
      voucherId,
    }: {
      readonly lineId: string
      readonly voucherId: string
    }) =>
      client.post<void>(
        `/api/v1/bank/statements/lines/${lineId}/actions/match`,
        { voucher_id: voucherId },
        { datasetCode },
      ),
    onSuccess: invalidate,
  })
}

export function useUnmatchLine() {
  const { client, datasetCode } = useSession()
  const invalidate = useInvalidateStatements()

  return useMutation({
    mutationFn: ({ lineId }: { readonly lineId: string }) =>
      client.post<void>(`/api/v1/bank/statements/lines/${lineId}/actions/unmatch`, undefined, {
        datasetCode,
      }),
    onSuccess: invalidate,
  })
}
