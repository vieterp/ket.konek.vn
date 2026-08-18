/**
 * Kiểu dùng chung của nhóm màn hình Danh mục & Thiết lập.
 *
 * `CatalogRow` là hình chiếu client của `MasterDataRow` phía server: bộ cột mà
 * **mọi** danh mục đều có (lát 3A). Cột riêng của từng danh mục (MST của đối
 * tác, tính chất của VTHH…) không nằm ở đây — chúng khai trong
 * `catalog-registry.ts` dưới dạng mô tả trường, và màn hình đọc chúng qua
 * `extraValue` thay vì ép kiểu bừa.
 */

import type { TranslationKey } from '@/locales/vi'

/** Bộ cột chung của mọi bản ghi danh mục — khớp `MasterDataRow` phía server. */
export interface CatalogRow {
  readonly id: number
  readonly uid: string
  readonly code: string
  readonly name: string
  readonly name_en: string | null
  readonly parent_id: number | null
  readonly path: string
  readonly level: number
  readonly is_group: boolean
  readonly is_active: boolean
  readonly branch_id: number | null
  readonly row_version: number
}

export interface CatalogPage {
  readonly items: readonly CatalogRow[]
  readonly total: number
}

/**
 * Một trường riêng của danh mục, đủ để drawer vẽ đúng ô nhập và gửi đúng kiểu.
 *
 * `lookup` là tham chiếu sang danh mục khác (ví dụ đối tác → điều khoản thanh
 * toán): ô nhập là `LookupInput`, giá trị gửi đi là `id`.
 */
export interface ExtraField {
  readonly key: string
  readonly labelKey: TranslationKey
  readonly type: 'text' | 'integer' | 'decimal' | 'checkbox' | 'select' | 'lookup'
  /** Cho `select`: danh sách giá trị đóng. */
  readonly options?: readonly { readonly value: string; readonly labelKey: TranslationKey }[]
  /** Cho `lookup`: slug danh mục đích. */
  readonly lookupSlug?: string
  /**
   * Trường chỉ khai lúc tạo (H69): có trong `POST`, không có trong `PUT`.
   * Drawer hiện nó ở chế độ sửa nhưng khóa lại, kèm lời giải thích.
   */
  readonly createOnly?: boolean
  /** Hiện ngay trong khối chính thay vì khối "Mở rộng" thu gọn (U2). */
  readonly essential?: boolean
  /** Giá trị khởi tạo khi tạo mới — trùng default của schema server. */
  readonly defaultValue?: string | boolean
}

/** Một giá trị của bộ lọc `?flag=` (H62). */
export interface CatalogFlagDef {
  readonly value: string
  readonly labelKey: TranslationKey
}

/** Mô tả một danh mục — bản khách của `CatalogSpec` trong registry server. */
export interface CatalogDef {
  /** Slug trên URL API — định danh ổn định (H49), trùng registry server. */
  readonly slug: string
  /** Đoạn đường dẫn tiếng Việt không dấu trên URL của client. */
  readonly urlSegment: string
  readonly titleKey: TranslationKey
  readonly flags: readonly CatalogFlagDef[]
  readonly extraFields: readonly ExtraField[]
  /** Khóa các trường riêng đáng hiện thành cột trong bảng (tối đa 2). */
  readonly listColumns: readonly string[]
}

/**
 * Đọc một trường riêng từ bản ghi mà không ép kiểu bừa: response của từng danh
 * mục có kiểu sinh từ OpenAPI, nhưng màn hình dùng chung thì chỉ biết
 * `CatalogRow` — phần còn lại đi qua đây và được thu hẹp tường minh.
 */
export function extraValue(row: CatalogRow, key: string): string | number | boolean | null {
  const value = (row as unknown as Record<string, unknown>)[key]
  if (value === null || value === undefined) {
    return null
  }
  if (
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean'
  ) {
    return value
  }
  return null
}
