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
| `DataGrid` | `data-grid/` | lưới **nhập liệu** nhiều dòng: một ô nhập tại một thời điểm + cuộn ảo, ô đang gõ không controlled (IME), dán vùng từ Excel, cột `readOnly` cho số server tính. Xem §Lưới nhập liệu bên dưới |
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
| — (design chưa vẽ màn nhập chứng từ) | `DataGrid` | dùng lại quy ước bảng của `.w2-t`: `th` nền `#F5F7FA`, cột số căn phải + `tabular-nums`, viền 1px `#C5D3E8`. Ô do server tính có nền `surface` |

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

### Lưới nhập liệu `DataGrid`

`client/src/design-system/components/data-grid/`. **Khác `DataTable`**: cái kia
là bảng chỉ-đọc của màn hình danh sách, cái này là chỗ gõ chứng từ nhiều dòng.
Không dùng lẫn.

Nền công nghệ do spike S3 chốt (lát 2C-3): **tự viết, không thư viện lưới**. Ba
quyết định làm nên hiệu năng của nó, và đây là ba thứ **không được gỡ**:

| # | Quyết định | Mất nó thì sao |
| --- | --- | --- |
| H11 | Đúng **một** `<input>` tồn tại tại một thời điểm; ô không được chọn chỉ là chữ. Cộng cuộn ảo (`use-row-window.ts`) | 500 dòng × 8 cột = 4.000 node có trạng thái riêng nằm trên đường tính toán mỗi phím gõ |
| H12 | Ô đang gõ **không controlled**; giá trị đi lên chỗ gọi khi rời ô (`commitMode='on-leave'`). Mọi phím **bỏ qua khi đang có tổ hợp IME** — cả hai vế (`compositionstart` của ta lẫn `isComposing` của sự kiện) | React ghi đè `value` giữa chừng một tổ hợp Telex là **nuốt dấu**; phím Enter kết thúc tổ hợp bị hiểu thành "xuống dòng dưới" |
| — | **Ô nhập bị tháo phải chốt giá trị trước** (layout effect đọc node đã rời DOM) | Trình duyệt **không** bắn `blur` khi phần tử đang focus bị gỡ khỏi DOM. Lăn chuột cho dòng đang gõ rời cửa sổ cuộn là **mất trắng** chữ vừa gõ, không một tín hiệu nào — đo được trên Chromium trước khi có đoạn này |
| H15 | Lưới **không tính tiền** — giá trị đi qua là chuỗi; cột "Thành tiền" khai `readOnly`, do server tính | Một phép nhân `number` trong JavaScript là một con số sai trên BCTC (LD-03) |

Số đo trên Chromium, lưới 500 dòng (ngưỡng: phím < 50ms, dán 200 dòng < 1s):

| Chế độ | p50 | p95 | max |
| --- | --- | --- | --- |
| `on-leave` (mặc định) | 12–14ms | ~17ms | 22–40ms |
| `live` (cam kết từng phím) | 8–12ms | ~16ms | 32–46ms |
| Dán 200 dòng × 5 cột | — | — | ~50ms (đo trong `onCommit`, có đối chứng đủ 1.000 ô) |

`commitMode='live'` chỉ dùng cho màn hình phải cộng dồn ngay khi gõ (bảng lương,
nguyên tắc U9) — nó đắt hơn hẳn và nằm sát trần hơn.

Bàn phím: `Tab`/`Shift+Tab` sang ô sửa được kế tiếp (bỏ qua cột `readOnly`, vắt
sang dòng sau, tới cuối lưới thì **thả** cho trình duyệt để ra được nút Lưu);
`Enter`/mũi tên dọc đi theo cột; mũi tên **ngang** di con trỏ trong chữ và chỉ
nhảy ô khi con trỏ đã ở mép; `Escape` trả ô về giá trị **lúc vào ô** (ở `live`
thì phát thêm một cam kết hoàn tác, vì những phím đã gõ đã đi lên chỗ gọi rồi).

**`rowKey` phải là định danh thật của bản ghi, không phải chỉ số dòng.** Đó là
hàng rào cho ca chỗ gọi đổi dữ liệu dưới chân ô đang gõ (xóa dòng bằng phím tắt,
dữ liệu về từ server): khóa đổi thì dòng và ô nhập được dựng lại, chữ chưa cam
kết bị bỏ — đúng thứ phải xảy ra, vì `rowIndex` giờ trỏ sang chứng từ khác.

Dán: `clipboard-tsv.ts` cài đúng quy tắc TSV của bảng tính (ô bọc ngoặc kép chứa
tab/xuống dòng). Dán **một ô đơn** để nguyên cho trình duyệt; vùng nhiều ô đi
lên chỗ gọi trong **một** lượt `onCommit`. Vùng dán dài hơn số dòng hiện có vẫn
báo đủ — **chỗ gọi tự nới dòng**, lưới không tự đẻ dòng vì nó không biết một
dòng chứng từ trống gồm những gì.

Đo lại: `make client-bench` (Playwright + Chromium thật, trang `/bench/data-grid`
chỉ có ở bản dev). CI có job riêng canh ngưỡng — xem `client-bench` trong
`.github/workflows/ci.yml`. Vì sao canh bằng CI: kết luận của spike chỉ đúng
chừng nào H11/H12/H15 còn nguyên, mà chúng là thứ một lần refactor thiện chí sẽ
gỡ mất, và triệu chứng — gõ hơi khựng — không ai nhìn thấy trong diff.

### Hai trang chỉ có ở bản dev

| Route | Dùng để | Tệp |
| --- | --- | --- |
| `/kitchen-sink` | Duyệt toàn bộ component trong đúng vỏ thật (token thật, chế độ tối thật, font offline thật) | `client/src/features/kitchen-sink/` |
| `/bench/data-grid` | Đo hiệu năng lưới nhập liệu (spike S3); tham số `?rows=`, `?mode=live` | `client/src/features/bench/` |

Cả hai gác bằng hằng lúc dựng **`__DEV_TOOLS__`** (`command === 'serve'` trong
`vite.config.ts`), **không** phải `import.meta.env.DEV`: cờ `DEV` đọc `NODE_ENV`,
nên `NODE_ENV=development pnpm build` từng cho ra một bản giao khách **có**
`/kitchen-sink` — một đường vào ứng dụng không qua `SessionGate` — kèm sourcemap
toàn bộ mã nguồn. Bất biến thật do `make client-bundle-check` canh (dựng thử
trong chính môi trường đó rồi grep bundle); `router.test.ts` canh phần mã.

Cả hai nằm **ngoài** `SessionGate` để mở `pnpm dev` là xem được ngay, không cần
server và không cần tài khoản — đó là toàn bộ lý do chúng tồn tại, và cũng là lý
do cổng dev phải chặt.

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

## 8. Cách thêm một báo cáo mới — chỉ bằng dữ liệu

Dành cho phase 6–10: một báo cáo mới là **bốn dòng metadata**, không phải một
module. Engine (`ket.reporting.engine`) không biết "Sổ Cái" là gì — nó chỉ chạy
những gì bốn bảng `report_*` (schema dataset) mô tả. Không restart server.

1. **Dataset** (`report_datasets`) — chỉ khi các dataset có sẵn không đủ.
   `sql_text` là một câu `SELECT` (được phép CTE/window function), **mọi giá
   trị động qua placeholder** khai trong `allowed_params`; cấm nối chuỗi. Nếu
   dataset trả dòng theo chi nhánh, khai `supports_branch` để lớp bọc thêm điều
   kiện — nhưng cô lập thật là RLS trên bảng gốc, lớp bọc chỉ là phòng thủ thứ
   hai. **Trần ≤30 dataset toàn hệ; 4 dataset đầu (gl_journal, gl_ledger,
   gl_detail, trial_balance) đang phục vụ 8 báo cáo** — thêm dataset mới phải
   trả lời được "vì sao không thêm cột vào dataset có sẵn".
2. **Layout** (`report_layouts`) — cột (key, nhãn vi/en, kiểu `text|date|money|
   quantity`, căn lề), nhóm + dòng tổng, khóa sắp xếp. Khóa nhóm phải là tiền
   tố của khóa sắp (bất biến kiểm lúc parse).
3. **Bộ tham số** (`report_param_sets`) — các ô NGOÀI bộ chuẩn (`from_date`,
   `to_date`, `ledger`, `branch_ids` luôn có sẵn). Client dựng form tự động từ
   spec này (`GET /reports/{code}/params`) — không sửa màn hình nào.
4. **Definition** (`report_definitions`) — mã (= **mã mẫu thông tư** nếu có),
   tên, nhóm hiển thị, trỏ ba mảnh trên, `ledger_scope`, và `package_id` của
   gói builtin cùng chế độ kế toán khi báo cáo thuộc một thông tư (catalog và
   render lọc theo scheme của dữ liệu — mã chéo thông tư trả 404).

Báo cáo builtin khai ở `kernel/config/reports/data/builtin_reports.json` +
`datasets/*.sql` (seed idempotent, probe `LIMIT 0` chạy chính câu SQL lúc
provision). Kết xuất lớn tự chuyển job nền theo ngưỡng
`report.{pdf,xlsx}_job_threshold_rows` — người thêm báo cáo không phải làm gì.

## 9. Cách sửa mẫu in — chỉ bằng dữ liệu

Mẫu in chứng từ là **dòng dữ liệu** trong `print_templates`: thân Jinja2
(sandbox) + `css_extra`, unique theo `(document_type, code)`, mỗi loại đúng một
mẫu mặc định. Sửa mẫu = sửa dòng; thêm mẫu = thêm dòng rồi đặt làm mặc định —
không sửa code, không restart. Biến ngữ cảnh của mẫu do tầng API ráp từ chính
`build_request` của nút Ghi sổ, nên không có đường "in một đằng ghi một nẻo".
Logo/cỡ chữ/số lẻ đọc từ settings (`report.*`, `format.*`) — logo gán bằng nút
một-bước ở màn Thiết lập. Mọi lần in ghi `print_log` (`copy_no` nối tiếp, cảnh
báo in lại); nháp in được mang watermark BẢN NHÁP trừ khi đơn vị tắt
`print.allow_draft_vouchers` (FR-RPT-011).
