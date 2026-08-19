/**
 * Tra hệ thống tài khoản cho lưới nhập chứng từ (`GET /api/v1/accounts`).
 *
 * Ba đường nạp, không đường nào trông vào "toàn bộ danh mục vừa một trang":
 * một lượt seed (trần 200 TK — `MAX_LIMIT` phía server) cho lưới hiện tên TK
 * ngay khi gõ; `resolve()` tra thêm đúng mã người dùng gõ khi mã rơi ngoài
 * trang đầu; và một lượt hydrate theo `ids=` cho form SỬA — chứng từ đã lưu
 * chỉ mang `account_id`, và TK của nó có thể xếp sau dòng 200 hoặc đã ngừng
 * dùng (review 4E, H-1).
 *
 * Bản đồ nạp lại từ đầu mỗi khi đổi ngày hạch toán: gói cấu hình hiệu lực
 * theo ngày (`on_date`), nên một TK hợp lệ ở ngày này có thể không còn ở gói
 * của ngày khác.
 */

import { useCallback, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type Account = Schemas['AccountResponse']

export interface AccountMaps {
  readonly byCode: ReadonlyMap<string, Account>
  readonly byId: ReadonlyMap<number, Account>
}

const EMPTY_MAPS: AccountMaps = { byCode: new Map(), byId: new Map() }

/** Trần một lượt đọc của `/api/v1/accounts` (`MAX_LIMIT` phía server). */
const SEED_LIMIT = 200

function mergeAccounts(maps: AccountMaps, accounts: readonly Account[]): AccountMaps {
  if (accounts.length === 0) {
    return maps
  }
  const byCode = new Map(maps.byCode)
  const byId = new Map(maps.byId)
  for (const account of accounts) {
    byCode.set(account.code.toLowerCase(), account)
    byId.set(account.id, account)
  }
  return { byCode, byId }
}

export interface AccountLookup {
  readonly maps: AccountMaps
  /** Tra thêm đúng mã đã gõ nếu chưa có trong bản đồ hiện tại — gọi khi rời ô "TK". */
  readonly resolve: (code: string) => void
  readonly isLoading: boolean
}

/** Bản đồ tra thêm, gắn NGÀY nó thuộc về — đổi ngày thì đọc ra coi như trống, không cần một lượt "dọn state" riêng. */
interface ExtraState {
  readonly postingDate: string
  readonly maps: AccountMaps
}

/** Trần số id một lượt hydrate (`MAX_IDS` phía server). */
const HYDRATE_IDS_LIMIT = 200

export function useAccountLookup(
  postingDate: string,
  requiredIds: readonly number[] = [],
): AccountLookup {
  const { client, datasetCode } = useSession()
  const [extraState, setExtraState] = useState<ExtraState>({ postingDate, maps: EMPTY_MAPS })
  // Chỉ đọc/ghi trong `resolve()` (một trình xử lý sự kiện, gọi khi rời ô
  // "TK") — KHÔNG đọc trong thân render, nên không phạm luật `react-hooks/refs`.
  // Khóa gắn cả ngày hạch toán (`${postingDate}:${key}`) để một lượt tra còn
  // treo từ ngày cũ không chặn tra lại đúng mã đó ở ngày mới.
  const pending = useRef(new Set<string>())

  const seed = useQuery({
    queryKey: ['accounts', datasetCode, postingDate, 'seed'],
    enabled: datasetCode !== null && postingDate !== '',
    queryFn: () =>
      client.get<Schemas['AccountListResponse']>(
        `/api/v1/accounts?on_date=${postingDate}&limit=${String(SEED_LIMIT)}`,
        { datasetCode },
      ),
  })

  // Hydrate theo id cho form SỬA: hệ TK thật vượt trần trang đầu, nên dòng cũ
  // trỏ vào TK xếp sau dòng 200 sẽ trống mã/tên nếu chỉ trông vào seed
  // (review 4E, H-1). `ids=` phía server gồm cả TK đã ngừng dùng — chứng từ cũ
  // vẫn phải hiện được nó. Khóa truy vấn theo danh sách id còn thiếu: khi seed
  // về đủ, danh sách rỗng và truy vấn tự tắt.
  const seededIds = new Set((seed.data?.items ?? []).map((account) => account.id))
  const missingIds = seed.isPending
    ? []
    : [...new Set(requiredIds)].filter((id) => !seededIds.has(id)).slice(0, HYDRATE_IDS_LIMIT)
  const hydrate = useQuery({
    queryKey: ['accounts', datasetCode, postingDate, 'ids', missingIds.join(',')],
    enabled: datasetCode !== null && postingDate !== '' && missingIds.length > 0,
    queryFn: () =>
      client.get<Schemas['AccountListResponse']>(
        `/api/v1/accounts?on_date=${postingDate}&limit=${String(HYDRATE_IDS_LIMIT)}&` +
          missingIds.map((id) => `ids=${String(id)}`).join('&'),
        { datasetCode },
      ),
  })

  const extra = extraState.postingDate === postingDate ? extraState.maps : EMPTY_MAPS
  const maps = mergeAccounts(
    mergeAccounts(mergeAccounts(EMPTY_MAPS, seed.data?.items ?? []), hydrate.data?.items ?? []),
    [...extra.byCode.values()],
  )

  const resolve = useCallback(
    (code: string): void => {
      const key = code.trim().toLowerCase()
      if (key === '' || datasetCode === null || postingDate === '') {
        return
      }
      const pendingKey = `${postingDate}:${key}`
      if (maps.byCode.has(key) || pending.current.has(pendingKey)) {
        return
      }
      pending.current.add(pendingKey)
      void client
        .get<Schemas['AccountListResponse']>(
          `/api/v1/accounts?on_date=${postingDate}&search=${encodeURIComponent(code.trim())}&limit=20`,
          { datasetCode },
        )
        .then((response) => {
          setExtraState((current) => ({
            postingDate,
            maps: mergeAccounts(
              current.postingDate === postingDate ? current.maps : EMPTY_MAPS,
              response.items,
            ),
          }))
        })
        .catch(() => undefined)
        .finally(() => {
          pending.current.delete(pendingKey)
        })
    },
    [client, datasetCode, postingDate, maps.byCode],
  )

  // `isLoading` phải chờ CẢ lượt hydrate theo id: form sửa dựng dòng đúng một
  // lần khi hết loading — dựng sớm hơn thì ô TK trống vĩnh viễn.
  const hydrating = missingIds.length > 0 && hydrate.isPending
  return { maps, resolve, isLoading: seed.isPending || hydrating }
}
