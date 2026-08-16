# Hướng dẫn thiết kế giao diện — Konek Két

## 1. Nguồn giao diện duy nhất

Design Konek Screens 2a + brand assets trong Claude Design project
`91a2c87a-3304-418f-a296-dc584cd56cfe`.

**Đọc chính file design, không đọc bản tóm tắt.** Truy cập bằng DesignSync MCP:

```
DesignSync list_files  projectId=91a2c87a-3304-418f-a296-dc584cd56cfe
DesignSync get_file    path="Konek Screens 2a.dc.html"
```

Khối `<style>` trong file đó (~4 KB) là **đặc tả chuẩn tắc** của bộ component —
xem bảng ánh xạ ở §5. `support.js` chỉ là runtime vẽ canvas, không phải nội dung
design. `.frame{border:2px solid}` là khung TRÌNH BÀY của canvas, **không** phải
viền thẻ trong ứng dụng (thẻ thật là `.w2-card`, viền 1px `#C5D3E8`).

Bản tóm tắt chữ ở
`plans/260814-2204-accounting-system-architecture/reports/design-reference-260814-2219-konek-screens-ui-direction-report.md`
chỉ để tra nhanh nguyên tắc UX. Lát 2C-2 cho thấy vì sao không được dùng nó thay
file gốc: nó giữ lại màu và nguyên tắc, nhưng làm mất giải phẫu component, thang
chữ, mật độ, từ vựng trạng thái và quy ước bảng — và bộ component đầu tiên dựng
theo nó đã lệch khỏi design ở sáu điểm, trong đó ba điểm phải làm lại.

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
| `client/src/design-system/base.css` (`@theme`) | Thang màu THÔ của brand (navy/ocean/sky/gray) + fontSize + fontFamily. Mỗi khai báo vừa sinh lớp tiện ích (`bg-navy-700`) vừa là CSS variable thật (`var(--color-navy-700)`). Cũng là nơi **phơi token ngữ nghĩa thành lớp tiện ích** (`bg-background`, `text-primary`…) |
| `client/src/design-system/tokens.css` | Token NGỮ NGHĨA `--ds-color-*` (text, surface, border, screen) + dark mode |

### Lớp tiện ích ngữ nghĩa — dùng cái này, không dùng mã màu thô

| Lớp | Token | Dùng cho |
| --- | --- | --- |
| `bg-screen` | `--ds-color-screen` | nền màn hình ứng dụng |
| `bg-background` | `--ds-color-background` | **nền thẻ / panel / bảng** |
| `bg-surface` | `--ds-color-surface` | nền sidebar, thanh trạng thái, đầu bảng |
| `text-text-default` / `text-text-muted` | `--ds-color-text*` | chữ thường / chữ phụ |
| `text-primary` · `border-primary` | `--ds-color-primary` | tiêu đề, viền nhấn, chữ nút phụ |
| `text-secondary` | `--ds-color-secondary` | link hành động trong bảng |
| `border-border-default` | `--ds-color-border` | đường kẻ |

> **Không viết `bg-white` hay `text-navy-700` cho nền thẻ và tiêu đề.** Hai màu
> đó **không** đổi theo chế độ sáng/tối. Lát 2C-1 thiếu hai lớp `bg-background`
> và `text-primary` nên mọi chỗ gọi đành dùng màu thô; đến khi bật chế độ tối
> lần đầu (2C-2) thì chữ sáng nằm trên thẻ trắng — không đọc được một dòng nào.
> Ngoại lệ có chủ đích duy nhất: nút `primary` giữ nền `bg-navy-700` đặc ở cả
> hai chế độ (chữ trắng trên navy đạt ~11:1; nếu đổi theo token thì ở chế độ tối
> nút thành ocean-400 và chữ trắng chỉ còn ~2,5:1).

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
| **secondary** | **ocean-700 `#2E6DB5`** | link hành động trong bảng (KHÔNG phải ocean-500 — xem khác biệt 4) |
| surface | `#F9FAFB` (gray-50) | nền sidebar / thanh trạng thái |
| background | `#FFFFFF` | nền thẻ / panel / bảng |
| **border** | **navy-100 `#C5D3E8`** | đường kẻ (KHÔNG phải gray-200 — xem khác biệt 3) |
| screen | `#F5F7FA` | nền màn hình ứng dụng |
| status ok / todo / bad | gray-500 · ocean-700 · `#B23A2A` | ba tông trạng thái, xem §5 |

`screen` **không có** trong `colors.css` — nó đến từ design Konek Screens 2a và
được thêm vào `tokens.css` với ghi chú nguồn.

### Bốn khác biệt có chủ đích so với brand asset

| # | Khác biệt | Lý do |
| --- | --- | --- |
| 1 | Bỏ `@import` font từ `fonts.googleapis.com` | Sản phẩm phải chạy **offline hoàn toàn** (LD-01). Font tự host bằng `@fontsource/be-vietnam-pro`, nạp trong `src/main.tsx`, subset **vietnamese**, 4 cân nặng 400/500/600/700 |
| 2 | Thêm token `--color-screen` | Nền màn hình ứng dụng, nguồn từ Konek Screens 2a |
| 3 | `border` = navy-100 `#C5D3E8`, không phải gray-200 `#E5E7EB` | Hai nguồn mâu thuẫn thật: mọi thẻ/bảng/ô nhập trong Konek Screens 2a dùng `1px solid #C5D3E8` — viền ánh navy. Màn hình thật thắng vì đó là thứ người dùng nhìn thấy |
| 4 | `secondary` = ocean-700, không phải ocean-500 | Ocean-500 trên nền trắng chỉ **3,34:1** — trượt WCAG AA cho chữ nhỏ, mà đây là màu nút hành động trong bảng ("Làm ngay"). Ocean-700 đạt **5,29:1** và trùng đúng `#2E6DB5` mà design dùng cho `.w2-note`/`.todo` |

`[data-theme="dark"]` bên cạnh `prefers-color-scheme` không tính là khác biệt về
giá trị — brand asset chỉ có media query, ta thêm đường cho người dùng chọn tay.

Khi đồng bộ tokens lần sau: giữ nguyên bốn khác biệt này, đừng kéo ngược bản gốc
đè lên.

### Typography & icon

- Font: **Be Vietnam Pro** 400/500/600/700.
- Bậc chữ **trang tiếp thị** (brand config): `hero` 3.5rem · `h1` 2.5rem ·
  `h2` 2rem · `h3` 1.5rem.
- Bậc chữ **màn hình nghiệp vụ** (Konek Screens 2a) — đặc hơn có chủ đích, vì
  đây là phần mềm nhập liệu dùng cả ngày và mỗi 1px thừa mỗi dòng là một dòng ít
  hơn nhìn thấy được:

  | Lớp | Cỡ | Dùng cho |
  | --- | --- | --- |
  | `text-page` | 21px/600 | tiêu đề trang |
  | `text-control` | 13.5px | nav, tab, ô nhập, nút |
  | `text-app` | 13px | nội dung bảng, thẻ |
  | `text-meta` | 12.5px | đầu bảng, nhãn trạng thái |

- Icon: **lucide**, 17px, `stroke-width` 1.75.
- Khung viền: thẻ/bảng/ô nhập **1px** `#C5D3E8`; gạch dưới topbar **2px** navy.
  (2px navy quanh cả thẻ là đọc nhầm `.frame` — khung trình bày của canvas — xem §1.)

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

### Bộ component hiện có

Thêm biến thể chỉ khi có màn hình thật đòi, không thêm trước:

| Component | Tệp | Biến thể / tính năng |
| --- | --- | --- |
| `Button` | `button.tsx` | `primary`, `secondary`, `ghost`; mặc định `type="button"` (mặc định của HTML là `submit`, và một nút phụ trong form sẽ gửi form) |
| `TextField` | `text-field.tsx` | nhãn `<label htmlFor>` thật, `hint`, `error`, `aria-invalid`/`aria-describedby` |
| `SelectField` | `select-field.tsx` | `<select>` gốc (bàn phím, IME tiếng Việt, trình đọc màn hình đã đúng sẵn), `labelHidden` cho thanh công cụ chật |
| `Alert` | `alert.tsx` | `error` (`role="alert"`), `warning`, `info` |
| `Tabs` | `tabs.tsx` | tab trạng thái kèm số việc; roving tabindex; **kích hoạt bằng tay** (mũi tên chỉ chuyển focus, Enter mới đổi tab — mỗi tab là một truy vấn xuống server) |
| `StatusPill` | `status-pill.tsx` | `neutral`, `pending`, `success`, `danger`; luôn có chữ, không chỉ có màu |
| `NextActionCell` | `next-action-cell.tsx` | ô "Việc tiếp theo" (U1); `action={null}` hiện nhãn "đã xong" chứ không để ô trống |
| `DataTable` | `data-table.tsx` | bảng **chỉ-đọc**: `align:'right'` + `tabular-nums` cho cột số, sắp xếp **do chỗ gọi điều khiển**, trạng thái rỗng/đang tải, `caption` bắt buộc |
| `Drawer` | `drawer.tsx` | panel phải qua portal; Esc đóng, **bấm ra nền KHÔNG đóng**; bẫy focus; trả focus về chỗ mở; focus vào ô đầu của thân |
| `SplitPane` | `split-pane.tsx` | chia trái/phải kéo bằng chuột **và bằng bàn phím** (`role="separator"`, mũi tên/Home/End); nhớ tỉ lệ qua `storageKey` |
| `ChecklistPanel` | `checklist-panel.tsx` | danh mục kiểm tra (U11); mỗi mục hỏng có đường dẫn tới chỗ sửa; câu tổng kết do chỗ gọi truyền vào |

### Ánh xạ đặc tả design → component

Cột trái là selector trong `Konek Screens 2a.dc.html`. Khi sửa component, đối
chiếu lại cột này chứ đừng đối chiếu trí nhớ.

| Design | Component | Ghi chú |
| --- | --- | --- |
| `.w2-b` / `.w2-b.pri` / `.w2-b.q` | `Button` secondary / primary / ghost | viền **1px**, `padding 8px 13px`, `gap 7px` |
| `.w2-in` + `.w2-f label` | `TextField`, `SelectField` | nhãn 12px xám; ô cao tối thiểu 36px |
| `.w2-tab` | `Tabs` | tab đang chọn gạch dưới 3px navy |
| `.seg` | `Seg` | chọn một giá trị, khác `Tabs` |
| `.w2-s` + `.ok`/`.todo`/`.bad` | `StatusPill` | chấm vuông 8px + chữ, **ba** tông |
| `.w2-t` / `th` / `td.n` / `tr.alt` / `tr.tot` | `DataTable` | `td.n` căn phải + `tabular-nums` + `nowrap`; `tr.tot` = prop `totals` |
| `.w2-card` + `.w2-ch` | `ChecklistPanel`, thẻ nói chung | viền 1px `#C5D3E8`, không phải 2px |
| `.w2-side` / `.w2-nav.on` | sidebar trong `app-layout` | rộng 212px; mục chọn = nền navy-50 + gạch trái 3px inset |
| `.w2-top` | topbar | gạch dưới **2px navy** |
| — (không có trong design) | `Drawer`, `SplitPane` | design không dùng overlay; xem ghi chú dưới |

**Design KHÔNG có drawer/modal/overlay nào** (0 lần trong cả file). Nguyên tắc là
drill-down **tại chỗ** (U10). `Drawer` được giữ cho những màn hình design chưa
vẽ (sửa nhanh một dòng), nhưng **cấm dùng cho drill-down sổ cái và BCTC** — hai
chỗ đó U10 bắt buộc mở tại chỗ.

### Ba tông trạng thái

Trục ngữ nghĩa là **"có việc cho bạn không"**, không phải "chứng từ ở trạng thái
nào". Đây là điểm dễ làm sai nhất:

| Tông | Màu (sáng / tối) | Nghĩa | Ví dụ |
| --- | --- | --- | --- |
| `ok` | gray-500 / gray-400 | xong, không phải làm gì | "Đã cấp mã", "Đã ghi sổ", "Khớp" |
| `todo` | ocean-700 / ocean-300 | cần bạn xử lý | "Chờ phát hành", "Chưa lập" |
| `bad` | `#B23A2A` / red-400 | hỏng, phải sửa | "Bị từ chối", "Lệch 31.600.000" |

**Không có tông "thành công" xanh lá.** Việc đã xong là **xám**: trên màn hình
200 dòng mà 190 dòng đã xong, tô xanh lá cho chúng làm 10 dòng còn việc chìm
nghỉm. Xám đẩy việc xong lùi khỏi tầm mắt và để màu dành cho thứ cần đọc.

**Bất biến của tầng design system: KHÔNG phụ thuộc `src/lib/`.** Không gọi
`useI18n`, không gọi `apiClient`, không dùng router — mọi chữ đi vào bằng prop.
Nhờ vậy component vẽ được trong test không cần provider, trang `/kitchen-sink`
mở được khi chưa đăng nhập, và cùng bộ này dùng lại được cho mẫu in render ở
server (ADR-009) nơi không có ngữ cảnh React.

**Lưới nhập liệu `DataGrid` chưa có** — nền công nghệ do spike S3 quyết (lát
2C-3). `DataTable` là bảng chỉ-đọc, không thay thế được.

### Trang duyệt `/kitchen-sink`

`client/src/features/kitchen-sink/` — toàn bộ component trong đúng vỏ thật
(token thật, chế độ tối thật, font offline thật). **Chỉ có ở bản dev**: gác bằng
`import.meta.env.DEV` trong `router.tsx` nên bản giao khách không có route này,
và có test khóa lại điều đó. Nằm **ngoài** `SessionGate` để người thiết kế mở
`pnpm dev` là xem được ngay, không cần server và không cần tài khoản.

Không dùng Storybook: ở đây component hiện trong chính cái vỏ sẽ chạy thật, và
không phải nuôi thêm một bộ công cụ thứ hai bên cạnh vitest + vite.

### Chế độ sáng/tối

`client/src/lib/theme.tsx` — ba lựa chọn `system` / `light` / `dark`, lưu ở
`localStorage` (`ket.theme`, thiết lập của **máy trạm**), đổi trong topbar.
`system` phải **gỡ** `data-theme` chứ không đặt `data-theme="system"`: một giá
trị lạ nằm đó sẽ loại `@media (prefers-color-scheme: dark)` mà không bật gì
thay thế — chế độ tối của hệ điều hành ngừng tác dụng, âm thầm.

### i18n cách sử dụng

Mọi chuỗi hiển thị lấy qua `t()` của hook `useI18n()`. Nguồn là hai module
TypeScript `client/src/locales/{vi,en}.ts` — không JSON, không i18next:

```typescript
const { t, locale, setLocale } = useI18n()

t('login.title')                              // "Đăng nhập"
t('common.version', { version: '0.6.0' })     // "Phiên bản 0.6.0"

// client/src/locales/vi.ts — nguồn khóa
export const vi = {
  'login.title': 'Đăng nhập',
  'common.version': 'Phiên bản {version}',
  'error.auth.invalid_credentials': 'Tên đăng nhập hoặc mật khẩu không đúng.',
} as const

export type TranslationKey = keyof typeof vi

// client/src/locales/en.ts — bộ khóa bị ép bằng kiểu
export const en: Record<TranslationKey, string> = {
  'login.title': 'Sign in',
  'common.version': 'Version {version}',
  'error.auth.invalid_credentials': 'Wrong username or password.',
}
```

Thiếu hay thừa một khóa trong `en.ts` là lỗi `tsc`. Mã lỗi của server đổi thành
câu hiển thị bằng `translateErrorCode(t, errorCode)` (tra khóa `error.<mã>`);
mã chưa có bản dịch rơi về câu chung **kèm chính mã đó**.

**Nguyên tắc:**
- `vi.ts` là nguồn khóa (khai `as const`)
- `en.ts` khai `Record<TranslationKey, string>` → TypeScript ép đủ khóa lúc biên dịch
- Thiếu khóa trong `en.ts` → lỗi `tsc`, không phải chuỗi lạ trên màn hình
- Số/tiền/ngày định dạng bằng `Intl` của trình duyệt (không cần i18n)

### Quy tắc khác

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
