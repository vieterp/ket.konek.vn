/**
 * Bản khách của registry danh mục — 22 danh mục, trùng một-một với
 * `server/src/ket/kernel/master_data/registry.py`.
 *
 * Vì sao chép tay thay vì sinh: thứ cần ở đây là **quyết định giao diện**
 * (trường nào hiện sẵn, trường nào vào khối "Mở rộng", cột nào đáng lên bảng,
 * nhãn hai thứ tiếng) — những thứ không có trong OpenAPI. Phần trùng với server
 * (slug, bộ route, cờ lọc) được canh bằng test đối chiếu `openapi.json`
 * (`catalog-registry.test.ts`), nên lệch registry là đỏ CI chứ không phải lỗi
 * chỉ lộ lúc chạy.
 *
 * Thứ tự trong `CATALOG_GROUPS` là thứ tự trên thanh chọn danh mục, gộp theo
 * mạch công việc của kế toán (SRS 01 §3–§9), không theo bảng chữ cái.
 */

import type { CatalogDef, CatalogFlagDef, ExtraField } from './catalog-types'
import type { TranslationKey } from '@/locales/vi'

const PARTNER_FLAGS: readonly CatalogFlagDef[] = [
  { value: 'customer', labelKey: 'catalog.flag.customer' },
  { value: 'vendor', labelKey: 'catalog.flag.vendor' },
]

const PARTNER_FIELDS: readonly ExtraField[] = [
  { key: 'is_customer', labelKey: 'catalog.field.isCustomer', type: 'checkbox', essential: true },
  { key: 'is_vendor', labelKey: 'catalog.field.isVendor', type: 'checkbox', essential: true },
  { key: 'tax_code', labelKey: 'catalog.field.taxCode', type: 'text', essential: true },
  {
    key: 'is_organization',
    labelKey: 'catalog.field.isOrganization',
    type: 'checkbox',
    defaultValue: true,
  },
  { key: 'address', labelKey: 'catalog.field.address', type: 'text' },
  { key: 'province', labelKey: 'catalog.field.province', type: 'text' },
  { key: 'district', labelKey: 'catalog.field.district', type: 'text' },
  { key: 'country', labelKey: 'catalog.field.country', type: 'text' },
  { key: 'contact_name', labelKey: 'catalog.field.contactName', type: 'text' },
  { key: 'phone', labelKey: 'catalog.field.phone', type: 'text' },
  { key: 'email', labelKey: 'catalog.field.email', type: 'text' },
  { key: 'website', labelKey: 'catalog.field.website', type: 'text' },
  { key: 'invoice_recipient', labelKey: 'catalog.field.invoiceRecipient', type: 'text' },
  { key: 'invoice_email', labelKey: 'catalog.field.invoiceEmail', type: 'text' },
  { key: 'credit_limit', labelKey: 'catalog.field.creditLimit', type: 'decimal' },
  {
    key: 'payment_term_id',
    labelKey: 'catalog.field.paymentTerm',
    type: 'lookup',
    lookupSlug: 'payment_terms',
  },
]

const PRICE_DIRECTION_OPTIONS: readonly { readonly value: string; readonly labelKey: TranslationKey }[] =
  [
    { value: '0', labelKey: 'catalog.field.priceDirectionPurchase' },
    { value: '1', labelKey: 'catalog.field.priceDirectionSale' },
  ]

const PRICE_LIST_FIELDS: readonly ExtraField[] = [
  // KHÔNG có `defaultValue`, cùng lối `nature` của vật tư hàng hóa: thân request
  // bỏ khóa khi ô để trống, và nút **nhóm** bảng giá bị server cấm mang chiều giá
  // (`group_has_no_pricing_fields`). Một giá trị mặc định ở đây sẽ khiến mọi lần
  // tạo nhóm gửi kèm `direction` rồi ăn 422.
  {
    key: 'direction',
    labelKey: 'catalog.field.priceDirection',
    type: 'select',
    options: PRICE_DIRECTION_OPTIONS,
    essential: true,
  },
  // Trỏ được vào **nút nhóm** đối tác — đó là cách FR-SAL-020 diễn đạt "nhóm
  // khách hàng", không cần cột thứ hai. Bỏ trống = áp cho mọi đối tác.
  {
    key: 'partner_id',
    labelKey: 'catalog.field.priceListPartner',
    type: 'lookup',
    lookupSlug: 'partners',
    essential: true,
  },
  {
    key: 'contract_id',
    labelKey: 'catalog.field.priceListContract',
    type: 'lookup',
    lookupSlug: 'contracts',
  },
  { key: 'effective_from', labelKey: 'catalog.field.effectiveFrom', type: 'date' },
  { key: 'effective_to', labelKey: 'catalog.field.effectiveTo', type: 'date' },
]

const EMPLOYEE_FIELDS: readonly ExtraField[] = [
  { key: 'department', labelKey: 'catalog.field.department', type: 'text', essential: true },
  { key: 'position', labelKey: 'catalog.field.position', type: 'text' },
  { key: 'id_number', labelKey: 'catalog.field.idNumber', type: 'text' },
  { key: 'tax_code', labelKey: 'catalog.field.personalTaxCode', type: 'text' },
  { key: 'phone', labelKey: 'catalog.field.phone', type: 'text' },
  { key: 'email', labelKey: 'catalog.field.email', type: 'text' },
  { key: 'bank_id', labelKey: 'catalog.field.bank', type: 'lookup', lookupSlug: 'banks' },
  { key: 'bank_account_number', labelKey: 'catalog.field.bankAccountNumber', type: 'text' },
  { key: 'bank_account_holder', labelKey: 'catalog.field.bankAccountHolder', type: 'text' },
]

const ITEM_NATURE_OPTIONS: readonly { readonly value: string; readonly labelKey: TranslationKey }[] =
  [
    { value: 'goods', labelKey: 'catalog.itemNature.goods' },
    { value: 'finished_goods', labelKey: 'catalog.itemNature.finishedGoods' },
    { value: 'service', labelKey: 'catalog.itemNature.service' },
    { value: 'description_only', labelKey: 'catalog.itemNature.descriptionOnly' },
  ]

const ITEM_FIELDS: readonly ExtraField[] = [
  {
    key: 'nature',
    labelKey: 'catalog.field.nature',
    type: 'select',
    options: ITEM_NATURE_OPTIONS,
    createOnly: true,
    essential: true,
  },
  {
    key: 'base_unit_id',
    labelKey: 'catalog.field.baseUnit',
    type: 'lookup',
    lookupSlug: 'units_of_measure',
    createOnly: true,
    essential: true,
  },
  {
    key: 'warehouse_id',
    labelKey: 'catalog.field.defaultWarehouse',
    type: 'lookup',
    lookupSlug: 'warehouses',
  },
  { key: 'description', labelKey: 'catalog.field.description', type: 'text' },
  // BA trạng thái (FR-SYS-043), nên `select` chứ không `checkbox`: ô trống =
  // "theo thiết lập hệ thống" và drawer bỏ hẳn khóa khỏi thân request, đúng thứ
  // server hiểu là `NULL`. Một checkbox chỉ nói được hai, và trạng thái thứ ba
  // biến mất.
  //
  // Trường này BẮT BUỘC có mặt ở đây dù không ai sửa nó thường xuyên: drawer dựng
  // thân request **chỉ** từ `extraFields`, nên một cột server sửa được mà client
  // không khai sẽ bị ghi về `NULL` mỗi lần người dùng sửa tên mã hàng — mất cờ
  // trong im lặng, và mọi chứng từ sau đó ra đơn giá cao hơn đúng một lần thuế
  // suất (review H-3 của lát 7C-1).
  {
    key: 'price_is_tax_inclusive',
    labelKey: 'catalog.field.priceIsTaxInclusive',
    type: 'select',
    options: [
      { value: 'true', labelKey: 'catalog.field.priceIsTaxInclusiveYes' },
      { value: 'false', labelKey: 'catalog.field.priceIsTaxInclusiveNo' },
    ],
  },
]

function simple(slug: string, urlSegment: string, titleKey: TranslationKey): CatalogDef {
  return { slug, urlSegment, titleKey, flags: [], extraFields: [], listColumns: [] }
}

/** 22 danh mục — trùng registry server, canh bằng test đối chiếu OpenAPI. */
export const CATALOGS: readonly CatalogDef[] = [
  {
    slug: 'partners',
    urlSegment: 'doi-tac',
    titleKey: 'catalog.title.partners',
    flags: PARTNER_FLAGS,
    extraFields: PARTNER_FIELDS,
    listColumns: ['tax_code', 'phone'],
  },
  {
    slug: 'employees',
    urlSegment: 'nhan-vien',
    titleKey: 'catalog.title.employees',
    flags: [],
    extraFields: EMPLOYEE_FIELDS,
    listColumns: ['department', 'position'],
  },
  {
    slug: 'items',
    urlSegment: 'vat-tu-hang-hoa',
    titleKey: 'catalog.title.items',
    flags: [],
    extraFields: ITEM_FIELDS,
    listColumns: ['nature'],
  },
  simple('warehouses', 'kho', 'catalog.title.warehouses'),
  simple('units_of_measure', 'don-vi-tinh', 'catalog.title.unitsOfMeasure'),
  simple('cost_objects', 'doi-tuong-chi-phi', 'catalog.title.costObjects'),
  simple('expense_items', 'khoan-muc-chi-phi', 'catalog.title.expenseItems'),
  simple('projects', 'cong-trinh', 'catalog.title.projects'),
  simple('project_types', 'loai-cong-trinh', 'catalog.title.projectTypes'),
  simple('contracts', 'hop-dong', 'catalog.title.contracts'),
  {
    slug: 'price_lists',
    urlSegment: 'bang-gia',
    titleKey: 'catalog.title.priceLists',
    sharedOnly: true,
    flags: [],
    extraFields: PRICE_LIST_FIELDS,
    listColumns: ['direction'],
  },
  {
    slug: 'asset_types',
    urlSegment: 'loai-tai-san',
    titleKey: 'catalog.title.assetTypes',
    flags: [],
    extraFields: [
      {
        key: 'default_useful_life_months',
        labelKey: 'catalog.field.defaultUsefulLifeMonths',
        type: 'integer',
      },
    ],
    listColumns: ['default_useful_life_months'],
  },
  {
    slug: 'tool_types',
    urlSegment: 'loai-ccdc',
    titleKey: 'catalog.title.toolTypes',
    flags: [],
    extraFields: [
      {
        key: 'default_allocation_months',
        labelKey: 'catalog.field.defaultAllocationMonths',
        type: 'integer',
      },
    ],
    listColumns: ['default_allocation_months'],
  },
  {
    slug: 'payment_terms',
    urlSegment: 'dieu-khoan-thanh-toan',
    titleKey: 'catalog.title.paymentTerms',
    flags: [],
    extraFields: [
      { key: 'due_days', labelKey: 'catalog.field.dueDays', type: 'integer', essential: true },
      { key: 'discount_days', labelKey: 'catalog.field.discountDays', type: 'integer' },
      { key: 'discount_percent', labelKey: 'catalog.field.discountPercent', type: 'decimal' },
    ],
    listColumns: ['due_days'],
  },
  {
    slug: 'banks',
    urlSegment: 'ngan-hang',
    titleKey: 'catalog.title.banks',
    flags: [],
    extraFields: [
      { key: 'short_name', labelKey: 'catalog.field.shortName', type: 'text', essential: true },
      { key: 'swift_code', labelKey: 'catalog.field.swiftCode', type: 'text' },
    ],
    listColumns: ['short_name'],
  },
  {
    // Danh mục thứ 21 (lát 6A) — TK ngân hàng CỦA DOANH NGHIỆP, khác bảng con
    // "TK ngân hàng của đối tác" trên trang đối tác. Mã = số tài khoản.
    slug: 'company_bank_accounts',
    urlSegment: 'tai-khoan-ngan-hang',
    titleKey: 'catalog.title.companyBankAccounts',
    flags: [],
    extraFields: [
      {
        key: 'bank_id',
        labelKey: 'catalog.field.bank',
        type: 'lookup',
        lookupSlug: 'banks',
        essential: true,
      },
      { key: 'currency_code', labelKey: 'catalog.field.currencyCode', type: 'text' },
      { key: 'account_holder', labelKey: 'catalog.field.bankAccountHolder', type: 'text' },
      { key: 'bank_branch', labelKey: 'catalog.field.bankBranchName', type: 'text' },
    ],
    listColumns: ['currency_code', 'account_holder'],
  },
  simple('document_types', 'loai-chung-tu', 'catalog.title.documentTypes'),
  simple('invoice_forms', 'mau-so-hoa-don', 'catalog.title.invoiceForms'),
  simple('timekeeping_symbols', 'ky-hieu-cham-cong', 'catalog.title.timekeepingSymbols'),
  simple('pit_tables', 'bieu-thue-tncn', 'catalog.title.pitTables'),
  simple('excise_tax_tables', 'bieu-thue-ttdb', 'catalog.title.exciseTaxTables'),
  simple('resource_tax_tables', 'bieu-thue-tai-nguyen', 'catalog.title.resourceTaxTables'),
]

/** Nhóm danh mục trên thanh chọn, theo mạch công việc (SRS 01 §3–§9). */
export const CATALOG_GROUPS: readonly {
  readonly labelKey: TranslationKey
  readonly slugs: readonly string[]
}[] = [
  { labelKey: 'catalog.group.partners', slugs: ['partners', 'employees'] },
  { labelKey: 'catalog.group.inventory', slugs: ['items', 'warehouses', 'units_of_measure'] },
  {
    labelKey: 'catalog.group.costing',
    slugs: ['cost_objects', 'expense_items', 'projects', 'project_types', 'contracts'],
  },
  { labelKey: 'catalog.group.assets', slugs: ['asset_types', 'tool_types'] },
  {
    labelKey: 'catalog.group.trading',
    slugs: [
      'payment_terms',
      'price_lists',
      'banks',
      'company_bank_accounts',
      'invoice_forms',
      'document_types',
    ],
  },
  {
    labelKey: 'catalog.group.payrollTax',
    slugs: ['timekeeping_symbols', 'pit_tables', 'excise_tax_tables', 'resource_tax_tables'],
  },
]

export function catalogBySlug(slug: string): CatalogDef | undefined {
  return CATALOGS.find((catalog) => catalog.slug === slug)
}

export function catalogByUrlSegment(segment: string): CatalogDef | undefined {
  return CATALOGS.find((catalog) => catalog.urlSegment === segment)
}
