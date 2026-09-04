/**
 * Cổng đối chiếu registry client ↔ hợp đồng OpenAPI của server.
 *
 * Registry ở client là bản chép tay có chủ đích (nó mang quyết định giao diện
 * mà OpenAPI không có), nên phần **trùng với server** phải được canh bằng máy:
 * lệch slug, thiếu danh mục mới, khai trường không tồn tại, hay khai cờ lọc mà
 * server không biết — tất cả phải đỏ ở đây chứ không phải lộ ra khi khách hàng
 * mở màn hình. Cùng vai với test "mọi danh mục đủ 6 route" phía server (H47).
 */

import { describe, expect, it } from 'vitest'

import openapi from '@api-types/openapi.json'

import { CATALOGS, CATALOG_GROUPS, catalogBySlug } from './catalog-registry'

const paths = Object.keys(openapi.paths)

/** Slug của mọi danh mục mà server phục vụ, suy từ chính bảng đường dẫn. */
const serverSlugs = new Set(
  paths
    .map((path) => /^\/api\/v1\/master\/([a-z_]+)$/.exec(path)?.[1])
    .filter((slug): slug is string => slug !== undefined),
)

type SchemaProperties = Record<string, unknown>

function schemaProperties(name: string): SchemaProperties {
  const schemas = (openapi as { components: { schemas: Record<string, { properties?: SchemaProperties }> } })
    .components.schemas
  return schemas[name]?.properties ?? {}
}

/** `PartnersCreateRequest` từ slug `partners` — đúng quy ước đặt tên của server. */
function pascal(slug: string): string {
  return slug
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('')
}

describe('registry danh mục đối chiếu OpenAPI', () => {
  it('mọi danh mục server phục vụ đều có mặt ở client, và ngược lại', () => {
    const clientSlugs = new Set(CATALOGS.map((catalog) => catalog.slug))
    expect([...clientSlugs].sort()).toEqual([...serverSlugs].sort())
  })

  it('mỗi danh mục có đủ bộ route CRUD + import/export', () => {
    for (const { slug } of CATALOGS) {
      const base = `/api/v1/master/${slug}`
      for (const suffix of [
        '',
        '/{record_id}',
        '/actions/merge',
        '/template',
        '/import/validate',
        '/import/commit',
        '/export',
      ]) {
        expect(paths, `${slug} thiếu route ${suffix || '(danh sách)'}`).toContain(base + suffix)
      }
    }
  })

  it('đoạn đường dẫn tiếng Việt là duy nhất', () => {
    const segments = CATALOGS.map((catalog) => catalog.urlSegment)
    expect(new Set(segments).size).toBe(segments.length)
  })

  it('mọi danh mục thuộc đúng một nhóm trên thanh chọn', () => {
    const grouped = CATALOG_GROUPS.flatMap((group) => group.slugs)
    expect(grouped.sort()).toEqual(CATALOGS.map((catalog) => catalog.slug).sort())
  })

  it('trường riêng khai ở client tồn tại trong thân tạo của server', () => {
    for (const { slug, extraFields } of CATALOGS) {
      const createProperties = schemaProperties(`${pascal(slug)}CreateRequest`)
      for (const field of extraFields) {
        expect(
          Object.hasOwn(createProperties, field.key),
          `${slug}: trường "${field.key}" không có trong CreateRequest`,
        ).toBe(true)
      }
    }
  })

  it('mọi trường server SỬA được đều khai ở client — thiếu một trường là mất dữ liệu', () => {
    // Chiều ngược của bài ngay trên, và nó tồn tại vì một lỗi cụ thể: drawer dựng
    // thân request `PUT` **chỉ** từ `extraFields`, nên một cột server sửa được mà
    // client không khai sẽ được gửi đi thiếu — và `None` mặc định của Pydantic ghi
    // đè giá trị đang có. Lát 7C-1 thêm `items.price_is_tax_inclusive` mà quên khai
    // ở đây; hậu quả là mỗi lần người dùng sửa **tên** một mã hàng, cờ "giá đã gồm
    // thuế" bị xóa và mọi chứng từ sau đó ra đơn giá cao hơn đúng một lần thuế suất.
    //
    // Bài cũ chỉ canh chiều "khai ở client thì phải có ở server" nên nó không thấy.
    const COMMON = new Set(['code', 'name', 'name_en', 'is_active', 'row_version'])
    for (const { slug, extraFields } of CATALOGS) {
      const declared = new Set(extraFields.map((field) => field.key))
      for (const key of Object.keys(schemaProperties(`${pascal(slug)}UpdateRequest`))) {
        if (COMMON.has(key)) {
          continue
        }
        expect(
          declared.has(key),
          `${slug}: server sửa được "${key}" nhưng client không khai — mỗi lần sửa ` +
            'bản ghi sẽ ghi đè cột đó về null',
        ).toBe(true)
      }
    }
  })

  it('trường chỉ-khai-lúc-tạo (H69) vắng mặt trong thân sửa; trường thường thì có', () => {
    for (const { slug, extraFields } of CATALOGS) {
      const updateProperties = schemaProperties(`${pascal(slug)}UpdateRequest`)
      for (const field of extraFields) {
        expect(
          Object.hasOwn(updateProperties, field.key),
          `${slug}: trường "${field.key}" ${field.createOnly === true ? 'phải vắng trong' : 'phải có trong'} UpdateRequest`,
        ).toBe(field.createOnly !== true)
      }
    }
  })

  it('cột hiện trên lưới đọc được từ thân phản hồi', () => {
    for (const { slug, listColumns } of CATALOGS) {
      const responseProperties = schemaProperties(`${pascal(slug)}Response`)
      for (const key of listColumns) {
        expect(
          Object.hasOwn(responseProperties, key),
          `${slug}: cột "${key}" không có trong Response`,
        ).toBe(true)
      }
    }
  })

  it('cờ lọc khai ở client khớp mô tả tham số `flag` của server', () => {
    interface FlagParameter {
      readonly name: string
      readonly description?: string
    }
    for (const { slug, flags } of CATALOGS) {
      const operation = (
        openapi.paths as Record<string, { get?: { parameters?: readonly FlagParameter[] } }>
      )[`/api/v1/master/${slug}`]
      const description =
        operation?.get?.parameters?.find((parameter) => parameter.name === 'flag')?.description ??
        ''
      const serverFlags = [...description.matchAll(/`(\w+)`/g)].map((match) => match[1])
      expect(
        flags.map((flag) => flag.value).sort(),
        `${slug}: cờ lọc lệch với server`,
      ).toEqual(serverFlags.sort())
    }
  })

  it('lookup trỏ tới danh mục có thật trong registry', () => {
    for (const { slug, extraFields } of CATALOGS) {
      for (const field of extraFields) {
        if (field.type === 'lookup') {
          expect(
            field.lookupSlug !== undefined && catalogBySlug(field.lookupSlug) !== undefined,
            `${slug}: lookup "${field.key}" trỏ tới slug lạ "${field.lookupSlug ?? ''}"`,
          ).toBe(true)
        }
      }
    }
  })
})
