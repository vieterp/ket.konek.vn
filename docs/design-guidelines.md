# Hướng dẫn thiết kế giao diện — Konek Két

## 1. Nguồn giao diện duy nhất

Design Konek Screens 2a + brand assets trong Claude Design project
`91a2c87a-3304-418f-a296-dc584cd56cfe`. Tóm tắt đã trích ở
`plans/260814-2204-accounting-system-architecture/reports/design-reference-260814-2219-konek-screens-ui-direction-report.md`.

Hai ràng buộc bắt buộc:

1. **Không tự sáng tác IA hay bảng màu.** Cần màn hình mới → đối chiếu design
   trước; không có thì hỏi, không tự nghĩ.
2. **Không sao chép giao diện phần mềm thương mại nào** — ràng buộc pháp lý nêu
   ở `docs/srs/README.md §Lưu ý khi triển khai`. Kiến thức miền lấy từ nghiên
   cứu thị trường được, giao diện thì không.

---

## 2. Tokens

Nguồn: `uploads/brand-assets/tokens/colors.css` v1.1 và
`uploads/brand-assets/tokens/tailwind.config.js` v1.0.

Đích trong repo:

| File | Vai trò |
| --- | --- |
| `client/src/design-system/base.css` (`@theme`) | Thang màu THÔ của brand (navy/ocean/sky/gray) + fontSize + fontFamily. Mỗi khai báo vừa sinh lớp tiện ích (`bg-navy-700`) vừa là CSS variable thật (`var(--color-navy-700)`) |
| `client/src/design-system/tokens.css` | Token NGỮ NGHĨA `--ds-color-*` (text, surface, border, screen) + dark mode |

**Không gõ tay lại mã màu.** Brand asset đổi → đồng bộ xuống, không sửa cục bộ.

> **Đổi từ Tailwind 4 (2026-08-15):** không còn `client/tailwind.config.js` —
> Tailwind 4 khai theme bằng `@theme` ngay trong CSS. Trước đó thang màu bị chép
> làm hai bản (một trong config JS, một trong `tokens.css`) và phải tự giữ đồng
> bộ với nhau; nay chỉ còn **một** nguồn.
>
> Tầng ngữ nghĩa đổi tiền tố `--color-*` → `--ds-color-*`: Tailwind 4 tự sinh
> `--color-*` từ `@theme`, nên để nguyên tên cũ sẽ thành `--color-screen:
> var(--color-screen)` — một vòng tự tham chiếu, CSS bỏ qua và màu nền biến mất.

### Màu chốt (kiểm tay khi đồng bộ)

| Token | Giá trị | Dùng cho |
| --- | --- | --- |
| navy-700 | `#1B365D` | primary — khung viền, tiêu đề, sidebar |
| ocean-500 | `#4A90D9` | secondary |
| sky-400 | `#7CB9E8` | accent |
| surface | `#F9FAFB` (gray-50) | nền thẻ / topbar |
| border | `#E5E7EB` (gray-200) | đường kẻ |
| screen | `#F5F7FA` | nền màn hình ứng dụng |

`screen` **không có** trong `colors.css` — nó đến từ design Konek Screens 2a và
được thêm vào `tokens.css` với ghi chú nguồn.

### Ba khác biệt có chủ đích so với brand asset

| # | Khác biệt | Lý do |
| --- | --- | --- |
| 1 | Bỏ `@import` font từ `fonts.googleapis.com` | Sản phẩm phải chạy **offline hoàn toàn** (LD-01). Font tự host bằng `@fontsource/be-vietnam-pro`, nạp trong `src/main.tsx`, subset **vietnamese**, 4 cân nặng 400/500/600/700 |
| 2 | Thêm token `--color-screen` | Nền màn hình ứng dụng, nguồn từ Konek Screens 2a |
| 3 | Thêm `[data-theme="dark"]` bên cạnh `prefers-color-scheme` | Người dùng chọn dark mode tay trong Thiết lập, không chỉ theo hệ điều hành |

Khi đồng bộ tokens lần sau: giữ nguyên ba khác biệt này, đừng kéo ngược bản gốc
đè lên.

### Typography & icon

- Font: **Be Vietnam Pro** 400/500/600/700.
- Bậc chữ: `hero` 3.5rem · `h1` 2.5rem · `h2` 2rem · `h3` 1.5rem (từ brand config).
- Icon: **lucide**.
- Khung viền đặc trưng: navy 2px.

---

## 3. IA màn hình ≠ ranh giới module backend

Đây là điểm dễ hiểu nhầm nhất. Backend giữ ranh giới module theo SRS; UI gộp
theo **công việc của người dùng**.

| Nhóm màn hình (thư mục `src/features/`) | Module backend | SRS |
| --- | --- | --- |
| `tien-vao-tien-ra` | `cash_book` + `bank` + `warehousing` (thủ quỹ) | 03, 04, 17 |
| `mua-hang` | `purchase` + `receivables` | 05 |
| `ban-hang` | `sales` + `receivables` | 06 |
| `hoa-don-dien-tu` | `einvoice` | 07, 08 |
| `kho` | `inventory` + `warehousing` (thủ kho) | 09, 17 |
| `tai-san` | `fixed_assets` + `tools` | 10, 11 |
| `luong` | `payroll` | 13 |
| `so-sach-thue` | `general_ledger` + `tax` + `costing` + `reporting` | 12, 14, 15, 18 |
| `danh-muc-thiet-lap` | `kernel` | 01, 02 |

**Quy tắc BFF:** một endpoint BFF tồn tại **khi và chỉ khi** một màn hình đọc
≥2 module. Màn hình chỉ đọc 1 module thì gọi thẳng router của module đó. BFF là
**đọc-only**; ghi luôn gọi API module. Bốn BFF chính đáng: `cashflow/overview`,
`assets/list`, `partners/{id}/overview`, `statements/financial-package` +
`period-close/checklist`.

---

## 4. Nguyên tắc UX bắt buộc giữ

Rút từ design reference. Các phase FE **tham chiếu** bảng này, không chép lại
chi tiết màn hình.

| # | Nguyên tắc | Phase |
| --- | --- | --- |
| U1 | Tab trạng thái nêu **việc còn thiếu**, không phải trạng thái kỹ thuật; có cột "Việc tiếp theo" | 6, 7, 8 |
| U2 | Form nhập liệu: **3 trường bắt buộc** hiện sẵn + khối "Mở rộng" thu gọn | 6, 7, 8, 9 |
| U3 | Trạng thái với cơ quan thuế là thông tin số 1 của HĐĐT — gộp **một cột** | 7 |
| U4 | Xử lý sai sót HĐĐT dạng **hỏi–đáp**, hệ thống tự chọn thay thế/điều chỉnh | 7 |
| U5 | Đối chiếu ngân hàng: sao kê trái ↔ sổ phải, dòng đã khớp mờ đi, gợi ý ghép | 6 |
| U6 | SoD thủ quỹ/thủ kho: đúng 3 việc (nhận đề nghị → thực hiện → ghi sổ) | 6, 8 |
| U7 | Tồn kho có cột **"Có thể bán"** = tồn − đã hứa giao | 8 |
| U8 | Kiểm kê: nhập số đếm → hệ thống chỉ chênh lệch → duyệt thì **tự tạo phiếu điều chỉnh** | 8 |
| U9 | Bảng lương = bảng tính có kiểm soát (ô nhập nền xanh nhạt, ô công thức có nhãn) | 9 |
| U10 | Sổ cái drill-down **tại chỗ** (mở dòng, không mở cửa sổ mới) | 10a |
| U11 | Khóa sổ = danh mục kiểm tra; mỗi lỗi dẫn thẳng tới chỗ sửa; ghi vết người khóa/mở | 10a |
| U12 | BCTC 4 báo cáo một trang, mỗi chỉ tiêu click về sổ cái, banner "đã đủ điều kiện lập" | 10a |
| U13 | Tờ khai GTGT đặt cạnh bảng kê đối chiếu; lệch thì chỉ ra chứng từ gây lệch | 9, 10a |
| U14 | Thiết lập nhóm theo hệ quả: **"đổi được bất kỳ lúc nào"** vs **"chốt một lần"** | 3, 5 |

---

## 5. Quy ước component

- Component cơ sở nằm ở `client/src/design-system/`; màn hình nghiệp vụ nằm ở
  `client/src/features/<nhóm>/`. Feature **không** định nghĩa lại nút/bảng riêng.
- Đặt tên file `kebab-case.tsx`, component `PascalCase`, hook `useXxx`.
- Không có phép tính tiền ở client — server trả số đã tính, client chỉ định dạng.
- Không gọi API Tauri trong luồng nghiệp vụ; cầu nối Tauri chỉ ở
  `src/lib/tauri/` (ép bằng eslint). Lý do: cùng bundle web này phải mở được
  bằng trình duyệt trong LAN ở v1.x.
- Lưới nhập liệu chứng từ nhiều dòng là rủi ro đã biết (500 dòng + gõ tiếng
  Việt qua IME phải không trễ) — spike **S3** ở phase 2 quyết thư viện grid.
  Plan-B: AG Grid Enterprise (**có phí**) hoặc Glide Data Grid.

---

## 6. Thêm màn hình mới — danh mục kiểm tra

1. Màn hình này thuộc nhóm nào trong bảng §3? Nếu không thuộc nhóm nào → dừng,
   hỏi lại, đừng tạo nhóm mới.
2. Đọc ≥2 module backend? Nếu không → gọi thẳng router module, **không** tạo BFF.
3. Có nguyên tắc UX nào ở §4 áp dụng? Ghi rõ trong PR.
4. Dùng token và component có sẵn; cần token mới → thêm ở brand asset trước.
5. Mọi chuỗi hiển thị đi qua i18n (vi/en), không hard-code trong logic.

---

## 7. Mẫu in (server-side)

Mẫu in PDF render **ở server** bằng WeasyPrint từ chính HTML/CSS + tokens của
design system này (ADR-009), nên bảng màu và font dùng chung với UI. Ràng buộc
bảo mật: Jinja2 chạy trong `SandboxedEnvironment`, `url_fetcher` của WeasyPrint
**chặn `file://`**. Chi tiết ở phase 5.
