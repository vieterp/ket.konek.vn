/**
 * Nhóm màn hình: Danh mục & Thiết lập (bước 19, lát 3D)
 *
 * SRS: 01, 02 · Module backend: kernel · Phase: 3, 4
 *
 * Thư mục theo NHÓM MÀN HÌNH của design, không theo ranh giới module backend
 * (xem bảng "IA màn hình ≠ ranh giới module" trong docs/system-architecture.md).
 *
 * Router cần các trang danh sách/thiết lập và ba trang chi tiết (đối tác, mã
 * hàng, bảng giá — hai trang sau từ 7H-2b); mọi thứ còn lại (drawer, wizard,
 * thẻ) là chi tiết bên trong của chúng.
 */

export { CatalogListPage } from './catalog-list-page'
export { ItemPage } from './item-page'
export { OpeningBalancePage } from './opening-balance-page'
export { PartnerPage } from './partner-page'
export { PriceListPage } from './price-list-page'
export { SettingsPage } from './settings-page'
