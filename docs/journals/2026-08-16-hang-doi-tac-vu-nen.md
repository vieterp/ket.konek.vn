# 2026-08-16 — Hàng đợi tác vụ nền & hợp đồng type OpenAPI (lát 2B-2b)

**Test:** 327 → 409 · **Bước:** 11 + 14 của phase 2 — lát cuối của 2B

Năm lát trước đều xảy ra **bên trong** một request: ai gọi, được làm gì, gửi lại
thì sao, hai người cùng sửa thì sao. Lát này mở đường ra ngoài request — việc
chạy hàng phút ở một tiến trình khác — và đóng hợp đồng type với client.

## Ranh giới khó nhất không phải kỹ thuật

Tiến trình chạy tác vụ nền phải **thấy job của mọi chi nhánh**, nếu không thì
không job nào chạy. Nhưng cô lập chi nhánh bằng RLS là thứ mọi truy vấn nghiệp
vụ dựa vào. Nới policy của bảng `jobs` cho ai cũng dùng được sẽ trả lời được câu
đầu và phá câu sau.

Đường đã chọn: một vai trò đăng nhập thứ tư (`ket_worker`) và một policy RLS
**chỉ áp cho nó**. Sau khi giành được việc, worker `SET LOCAL ROLE ds_<mã>_app`
rồi đặt phạm vi chi nhánh theo chính dòng job — từ đó thân job chịu đúng bộ
quyền và đúng RLS như một request HTTP. Ngoại lệ rộng đúng bằng lượt giành việc,
không hơn.

Điều đáng ghi là cách kiểm nó: một loại job "thăm dò" chạy trong worker thật và
báo cáo `current_user` cùng `current_setting('ket.branch_ids')`. Kiểm khai báo
thì chỉ chứng minh mã đã viết đúng ý; kiểm thế này chứng minh **PostgreSQL đang
thấy** đúng thứ đó.

## Bài học đắt nhất: chặn giành việc không phải là chặn chạy việc

`SELECT … FOR UPDATE SKIP LOCKED` bảo đảm hai worker không **giành** cùng một
job. Cả lát này viết xong với niềm tin đó là đủ. Review thù địch đo được phần
còn lại:

    A giành → A treo (GC, mất mạng) → lease hết hạn → reaper xếp lại hàng
    → B giành → A tỉnh lại, gia hạn lease **thành công**, ghi kết quả của mình

Job báo "xong" với kết quả của A trong khi B vẫn đang chạy. Ở phase 8 đó là một
bảng phân bổ khấu hao ghi hai lần.

Lỗ hổng nằm ở một chỗ nhỏ: `heartbeat` hỏi *"job có đang chạy không"* thay vì
*"job này có phải của tôi không"*. Sau khi B giành lại, câu hỏi thứ nhất trả lời
"có" — đúng như A mong đợi.

Cách sửa cũng nhỏ: `attempt` (số hiệu lượt giành, vốn đã có sẵn để đếm số lần
thử) trở thành **hàng rào**, đi vào mệnh đề `WHERE` của mọi lệnh ghi trạng thái.
Lượt chạy cũ khớp `id`, khớp `status`, nhưng không khớp số hiệu — nên nó không
ghi được dòng nào.

Điều học được không phải "nhớ dùng fence token", mà là: **một cơ chế đồng thời
chỉ chặn được đúng câu hỏi nó đặt ra.** Câu hỏi của `SKIP LOCKED` là "ai giành
được", và nó trả lời hoàn hảo. Câu hỏi "ai đang giữ" thì chưa ai hỏi.

## Ba lỗ khác cùng một họ

Review còn đo được ba thứ nữa, và cả ba đều là *câu hỏi chưa được hỏi*:

* **`finish` không hỏi job đã kết thúc chưa** — một job đã hủy bị biến thành
  "hoàn thành", và `finished_at` bị ghi đè nên không còn dấu vết lần hủy.
* **Quyền `UPDATE` của worker không hỏi cột nào** — cấp toàn bảng nghĩa là sửa
  được cả `type` và `params` của một job đang chờ, mà worker thì không kiểm lại
  quyền lúc chạy. Nay là quyền theo cột: worker ghi được *về lượt chạy của
  mình*, không ghi được *việc phải làm*.
* **Vòng lặp worker không hỏi "nếu DB chớp thì sao"** — một `OperationalError`
  lúc PostgreSQL khởi động lại giết cả tiến trình, và trên bản cài không có ai
  trực thì hàng đợi đứng tới khi có người để ý.

## Một quyết định của người dùng, siết lại bằng bằng chứng

Người dùng chọn đưa cả việc dọn phiên đăng nhập vào hàng đợi. Lúc chốt, rủi ro
đã được nêu: vai trò runtime **cố ý** không có `DELETE` trên `auth_sessions`, để
một lỗ hổng ở tầng API không xóa được dấu vết ai đã đăng nhập.

Thi công theo hướng fail-closed: job chỉ chạy khi bản cài khai tường minh một
DSN owner riêng cho worker, mặc định không có.

Review sau đó đo thêm một chiều mà cả hai bên đều chưa nghĩ tới: bảng
`role_permissions` nằm **trong schema từng dữ liệu kế toán**, nên một mã quyền
dùng chung sẽ cho người chỉ quản trị doanh nghiệp B chạy được thao tác xóa lịch
sử đăng nhập của **mọi** người. Phạm vi của quyền và phạm vi của hậu quả lệch
nhau một tầng.

Nay nó có mã quyền riêng (`system.installation.create`, đòi 2FA), sàn lưu trữ
bảy ngày, và một danh sách đóng các loại job được phép chạy dưới kết nối đặc
quyền — kiểm ngay lúc đăng ký, nên từ phase 4 không module nào tự cấp quyền owner
cho mình được. Lựa chọn sạch hơn (bỏ hẳn, chỉ giữ ở CLI) vẫn để người dùng chốt:
nó lật một quyết định họ đã đưa ra.

## Ghi chú quy trình, phải trả bằng thời gian thật

Reviewer chạy kiểm đột biến **trên cùng cây mã và cùng cụm PostgreSQL** với
controller. Hai hệ quả:

* bản vá cho lỗi CRITICAL bị bộ hoàn nguyên của reviewer ghi đè **hai lần** —
  phát hiện bằng `git diff`, không phải bằng test (test cũng đang đỏ vì lý do
  khác);
* hai tiến trình `pytest` song song phá nhau qua `conftest._drop_ket_roles`, vốn
  xóa và dựng lại vai trò ở phạm vi **cụm** — sinh ra một loạt lỗi giả mà cả hai
  bên đều đuổi theo một lúc.

Lần sau: reviewer chạy trong `git worktree` riêng và một cụm PostgreSQL riêng.
Rẻ hơn nhiều so với một lần ngồi dò xem bản vá của mình còn nguyên không.

## Còn lại của phase 2

Chỉ còn lát 2C: client (màn hình đăng nhập, layout, lưới nhập liệu) và ba spike
bắt buộc — esign, DataGrid 500 dòng gõ tiếng Việt, đóng gói server Python kèm
native deps. Hợp đồng type đã sinh sẵn, nên client bước vào không phải đoán hình
dạng nào của API.
