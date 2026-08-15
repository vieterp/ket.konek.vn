# 2026-08-15 — Phân quyền & định tuyến request (lát 2B-1b)

**Phiên bản:** 0.3.0 → 0.4.0 · **Test:** 203 → 265 · **Nhánh:** `feature/phase-02b1b-authorization`

Lát này trả lời câu hỏi thứ hai của bộ đôi danh tính: 2B-1a hỏi "ai đăng nhập",
đây hỏi "được làm gì, ở dữ liệu kế toán nào".

## Ba quyết định định hình mã

**Mã quyền được sinh, không gõ tay.** Registry loại chứng từ → `{module}.{chứng
từ}.{hành vi}`. Rủi ro ghi trong plan là "RBAC thiếu chiều loại chứng từ → phase 6
phải đổi schema quyền"; cách chặn nó là để bảng `permissions` chỉ còn là **ảnh
chiếu** của một cấu trúc trong bộ nhớ. Thêm chứng từ mới = thêm một `DocumentType`,
không migration nào cả.

**Cấm tường minh thắng cho phép.** Không phải để linh hoạt, mà để "kế toán viên
nhưng không được xóa chứng từ" là một vai trò **cấm thêm** chứ không phải bản sao
của vai trò gốc thiếu vài dòng — bản sao sẽ trôi khỏi bản gốc ở lần sửa quyền tiếp
theo, và không ai phát hiện.

**Mặc định của mọi cổng là chặn.** Ba trạng thái phiên (thiếu `X-Dataset`, phiên
hạn chế 2FA, mật khẩu tạm) đều đóng theo mặc định trong `principal_dependency`.
Endpoint mới không khai gì sẽ bị chặn cả ba. Hướng ngược lại — mở theo mặc định —
là loại lỗi chỉ lộ ra khi đã có người khai thác.

## Thứ tự fail-safe: chỗ duy nhất không thể làm nguyên tử

`users.totp_required` là cờ **toàn cục** (đăng nhập chạy trước khi chọn dataset)
còn vai trò là **per-dataset**. Gán một vai trò nhạy cảm vì thế chạm hai schema,
và hai schema nghĩa là hai transaction — không có cách nào làm nó nguyên tử.

Cái duy nhất chọn được là **hướng hỏng**: bật cờ trước, ghi vai trò sau. Hỏng giữa
chừng để lại "bị đòi 2FA mà chưa có quyền gì thêm" — phiền, tự sửa được. Thứ tự
ngược lại để lại một tài khoản quản trị **không** bị đòi lớp thứ hai, im lặng.

Quyết định đó kéo theo một hệ quả bảo mật không nhìn thấy trước: vì cờ do
transaction điều khiển đặt, vai trò dataset (`ds_*_app`) **không cần** quyền ghi
nào trên `public.users`. Đó chính là điều kiện để đóng M1 của vòng review trước —
một câu hỏi treo từ lát 2B-1a được trả lời bởi một quyết định về thứ tự transaction.

## Điểm chặn H4 đã đóng

Tài khoản bị bắt bật 2FA mà chưa đăng ký thiết bị thì không đăng nhập được, mà
không đăng nhập được thì cũng không gọi được endpoint đăng ký. Lát trước chỉ có
đường thoát tại máy chủ — tức là mỗi lần gán vai trò nhạy cảm là một cuộc gọi hỗ trợ.

Nay: phiên hạn chế `scope='totp_enrollment'`, chỉ mở đúng hai endpoint đăng ký, và
tự thu hồi ngay sau khi xác nhận (phiên cấp cho một tài khoản **chưa** qua lớp thứ
hai thì không được tự nâng cấp). Vòng "đăng nhập → đăng ký → xác nhận → đăng nhập
lại với mã" đi trọn bằng HTTP.

## Review thù địch bắt được thứ mình không nghĩ tới

30 đột biến, bắt 24 (80%). 0 CRITICAL. Nhưng một HIGH đáng nhớ:

`assign_branch` dựng phạm vi chi nhánh cho transaction ghi từ chi nhánh **đích**,
không phải từ phạm vi của người thực hiện. Lý do rất hợp lý lúc viết — dòng
`user_branches` mang `branch_id`, listener nhật ký lấy chi nhánh của *bản ghi*, nên
`WITH CHECK` của policy đòi đúng chi nhánh đó phải có trong GUC. Giải quyết được
ràng buộc kỹ thuật, và vô tình mở ra: ai nắm `system.user.edit` tự gán được **mọi**
chi nhánh cho chính mình. Reviewer đo qua HTTP thật: phạm vi `[2]` → `[2, 3]`, HTTP 200.

Điều đáng ghi không phải lỗi, mà là hình dạng của nó: một ràng buộc kỹ thuật thật
(RLS `WITH CHECK`) được giải bằng cách nới đúng thứ đang bảo vệ, và dòng nhật ký
của thao tác đó mang chi nhánh **đích** nên vô hình với chính người vừa bị vượt qua.
Lớp phòng thủ tự che dấu vết của việc mình bị vượt.

Hai lỗ còn lại là lỗ **test**, không phải lỗ hành vi: bỏ hẳn `require_permission` ở
`/audit-log` và bỏ hẳn kiểm mật khẩu ở `/totp/enroll` đều đi qua 251 test xanh. Mã
đúng, nhưng không có gì giữ nó đúng qua lần refactor sau.

## Lỗi lặp lại: đọc–sửa–ghi không khóa hàng

Lát trước có hai CRITICAL đúng loại này. Lát này có ba chỗ nữa (`grant_role`,
`assign_branch`, gieo mầm) — 4 luồng song song cho 2–3 `UniqueViolation`, tức HTTP
500 cho đúng thao tác mà `GrantResponse` vừa hứa là gọi lại được.

Cách sửa không dùng `ON CONFLICT DO NOTHING` dù nó gọn hơn: câu upsert là SQL Core,
không đi qua listener nhật ký, nên nó sẽ ghi quyền mà **không để lại vết**. Khóa
dòng cha (`roles`/`branches`) đắt hơn một chút và giữ được FR-NFR-012.

Đây là lần thứ hai cùng một hình dạng lỗi xuất hiện ở một lát khác. Nó nên vào danh
sách kiểm khi review, không phải chờ được phát hiện lại.

## Còn nợ

- Thêm quyền nhạy cảm vào vai trò **đã có người giữ** không bật cờ 2FA cho họ. Chưa
  hở hôm nay (nguồn ghi `role_permissions` duy nhất cấp toàn bộ quyền ngay từ đầu);
  phase 5 mở đúng đường này — đã ghi thành ràng buộc trong docstring.
- `/system/datasets` không lọc theo vai trò: trả về mọi dữ liệu kế toán đang hoạt
  động cho bất kỳ ai đã đăng nhập. Lọc đòi mở từng schema.
- Rate limit đầy đủ vẫn chưa có bước nào sở hữu; lát này mới đặt trần số lần băm
  Argon2id đồng thời.
