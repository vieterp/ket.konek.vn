/**
 * Tra danh mục LỚN bằng `search=`/`ids=` server-side trên `GET /api/v1/master/{slug}`
 * (hợp đồng 6A, cùng khuôn `/accounts`) — trả nợ 6A "client lookup chuyển sang
 * search=/ids=".
 *
 * Khác `useLookupOptions` (nạp trọn — chỉ đúng với danh mục nhỏ): ở đây chỉ
 * seed một trang đầu để ô lookup có gợi ý ngay, phần còn lại tra theo chuỗi
 * đang gõ (debounce trong hook) và tra bù theo `ids=` cho form SỬA — bản ghi
 * của chứng từ cũ có thể xếp sau trang đầu hoặc đã ngừng theo dõi.
 *
 * **Bản DUY NHẤT** từ lát 6G-2 (nợ 6F-2): `so-sach-thue/use-dimension-lookups`
 * từng giữ một `useSlugLookup` gần như y hệt — cùng seed, cùng tra bù `ids=`,
 * cùng bộ nhớ phụ — khác đúng một điều: nó tra theo MÃ và **chờ** kết quả (rà
 * lúc Cất), còn bản này tra theo chuỗi đang gõ và không chờ. Hai nhu cầu ấy là
 * hai HÀM trên cùng một hook, không phải hai hook; giữ hai bản là giữ hai chỗ
 * phải vá mỗi khi hợp đồng `/master/{slug}` đổi (và 6F-2 đã phải vá cả hai).
 *
 * Ở `danh-muc-thiet-lap` chứ không trong nhóm màn hình nào: cả nhóm 03 (Tiền
 * vào tiền ra) lẫn nhóm 09 (Sổ sách – thuế) đều gọi, và nó đọc chính hợp đồng
 * danh mục — `catalog-types` đã ở đây.
 */

import { useCallback, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import type { LookupOption } from '@/design-system/components'
import { useSession } from '@/lib/session'

import type { CatalogPage, CatalogRow } from '@/features/danh-muc-thiet-lap/catalog-types'

/** Trần một lượt đọc — MAX_PAGE_SIZE phía server (H52). */
const SEED_LIMIT = 200
const SEARCH_LIMIT = 20
const SEARCH_DEBOUNCE_MS = 250

export interface MasterSearchLookup {
  /** Danh sách đã gộp (seed + kết quả tra) — nguồn cho `LookupInput.options`. */
  readonly options: readonly LookupOption[]
  readonly byId: ReadonlyMap<number, LookupOption>
  /** Gọi mỗi lần chuỗi gõ đổi — hook tự debounce rồi tra server. */
  readonly searchFor: (query: string) => void
  /**
   * Tra một loạt MÃ và **chờ** kết quả, gộp vào bộ nhớ phụ rồi trả phần tra
   * được. Dùng ở đường rà-lúc-Cất: ô nhập cầm một mã người dùng gõ tay, cần
   * biết ngay nó có thật hay không — khác hẳn `searchFor` (gợi ý trong lúc gõ,
   * hỏng thì im lặng).
   */
  readonly fetchCodes: (codes: readonly string[]) => Promise<readonly LookupOption[]>
  readonly isLoading: boolean
}

function toOptions(rows: readonly CatalogRow[]): LookupOption[] {
  // Nút nhóm bị loại (không phải đích tham chiếu) — cùng luật `useLookupOptions`.
  return rows
    .filter((row) => !row.is_group)
    .map((row) => ({ id: row.id, code: row.code, label: row.name }))
}

function mergeOptions(
  base: ReadonlyMap<number, LookupOption>,
  extra: readonly LookupOption[],
): ReadonlyMap<number, LookupOption> {
  if (extra.length === 0) {
    return base
  }
  const next = new Map(base)
  for (const option of extra) {
    next.set(option.id, option)
  }
  return next
}

/**
 * Kết quả tra thêm, gắn SLUG **và** BỘ DỮ LIỆU nó thuộc về — đổi slug
 * (partners↔employees) hay đổi dataset thì đọc ra coi như trống, id của hai
 * bảng/hai bộ trùng dải không trộn được vào nhau (review 6F-1 M-C; hai bản
 * tiền-6G-2 mỗi bản chỉ gắn MỘT trong hai khóa, nên bản gộp gắn cả hai).
 */
interface ExtraState {
  readonly slug: string
  readonly datasetCode: string | null
  readonly map: ReadonlyMap<number, LookupOption>
}

export function useMasterSearchLookup(
  slug: string,
  requiredIds: readonly number[] = [],
): MasterSearchLookup {
  const { client, datasetCode } = useSession()
  const [extraState, setExtraState] = useState<ExtraState>({
    slug,
    datasetCode: null,
    map: new Map(),
  })
  const extra =
    extraState.slug === slug && extraState.datasetCode === datasetCode
      ? extraState.map
      : new Map<number, LookupOption>()
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null)

  const seed = useQuery({
    queryKey: ['catalog', datasetCode, slug, 'search-lookup-seed'],
    enabled: datasetCode !== null,
    queryFn: async () => {
      const page = await client.get<CatalogPage>(
        `/api/v1/master/${slug}?limit=${String(SEED_LIMIT)}`,
        { datasetCode },
      )
      return toOptions(page.items)
    },
  })

  // Tra bù theo id cho form SỬA — bản ghi trên chứng từ cũ có thể nằm ngoài
  // trang seed hoặc đã ngừng theo dõi; thiếu lượt này thì ô lookup trống và
  // lượt lưu kế tiếp xóa lặng lẽ tham chiếu (cùng họ review 4E H-1).
  const seededIds = new Set((seed.data ?? []).map((option) => option.id))
  const missingIds = seed.isPending
    ? []
    : [...new Set(requiredIds)].filter((id) => !seededIds.has(id) && !extra.has(id))
  const hydrate = useQuery({
    queryKey: ['catalog', datasetCode, slug, 'search-lookup-ids', missingIds.join(',')],
    enabled: datasetCode !== null && missingIds.length > 0,
    queryFn: async () => {
      const page = await client.get<CatalogPage>(
        `/api/v1/master/${slug}?limit=${String(SEED_LIMIT)}&` +
          missingIds.map((id) => `ids=${String(id)}`).join('&'),
        { datasetCode },
      )
      return toOptions(page.items)
    },
  })

  const merged = mergeOptions(
    mergeOptions(new Map((seed.data ?? []).map((option) => [option.id, option])), [
      ...(hydrate.data ?? []),
    ]),
    [...extra.values()],
  )

  const searchFor = useCallback(
    (query: string): void => {
      const needle = query.trim()
      if (debounce.current !== null) {
        clearTimeout(debounce.current)
        debounce.current = null
      }
      if (needle === '' || datasetCode === null) {
        return
      }
      debounce.current = setTimeout(() => {
        void client
          .get<CatalogPage>(
            `/api/v1/master/${slug}?search=${encodeURIComponent(needle)}&limit=${String(SEARCH_LIMIT)}`,
            { datasetCode },
          )
          .then((page) => {
            setExtraState((current) => ({
              slug,
              datasetCode,
              map: mergeOptions(
                current.slug === slug && current.datasetCode === datasetCode
                  ? current.map
                  : new Map(),
                toOptions(page.items),
              ),
            }))
          })
          .catch(() => undefined)
      }, SEARCH_DEBOUNCE_MS)
    },
    [client, datasetCode, slug],
  )

  const fetchCodes = useCallback(
    async (codes: readonly string[]): Promise<readonly LookupOption[]> => {
      if (datasetCode === null || codes.length === 0) {
        return []
      }
      const settled = await Promise.all(
        codes.map(async (code) => {
          try {
            const page = await client.get<CatalogPage>(
              `/api/v1/master/${slug}?search=${encodeURIComponent(code)}&limit=${String(SEARCH_LIMIT)}`,
              { datasetCode },
            )
            return toOptions(page.items)
          } catch {
            // Mạng đứt giữa lượt tra bù: trả rỗng để lượt rà thứ hai báo "mã
            // không tra được" — một thông điệp, một đường xử lý.
            return []
          }
        }),
      )
      const fetched = settled.flat()
      if (fetched.length > 0) {
        setExtraState((current) => ({
          slug,
          datasetCode,
          map: mergeOptions(
            current.slug === slug && current.datasetCode === datasetCode ? current.map : new Map(),
            fetched,
          ),
        }))
      }
      return fetched
    },
    [client, datasetCode, slug],
  )

  const hydrating = missingIds.length > 0 && hydrate.isPending
  return {
    options: [...merged.values()],
    byId: merged,
    searchFor,
    fetchCodes,
    isLoading: seed.isPending || hydrating,
  }
}
