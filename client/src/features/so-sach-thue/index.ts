/**
 * Nhóm màn hình: Sổ sách & Thuế
 *
 * SRS: 12, 14, 15, 18 · Module backend: general_ledger + tax + costing + reporting · Phase: 9, 10a
 *
 * Thư mục theo NHÓM MÀN HÌNH của design, không theo ranh giới module backend
 * (xem bảng "IA màn hình ≠ ranh giới module" trong docs/system-architecture.md).
 *
 * Lát 4E dựng ba màn hình đầu tiên của nhóm: danh sách chứng từ, form chứng từ
 * nghiệp vụ khác, và bảng cân đối tài khoản — phần còn lại (thuế, chi phí, báo
 * cáo) hoãn tới phase 9/10a.
 */

export { VoucherListPage } from './voucher-list-page'
export { JournalVoucherForm } from './journal-voucher-form'
export { TrialBalancePage } from './trial-balance-page'
export { ReportCatalogPage } from './report-catalog-page'
