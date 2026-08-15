# 2026-08-15 — Hợp đồng ghi (lát 2B-2a)

**Phiên bản:** 0.4.0 → 0.5.0 · **Test:** 265 → 327 · **Nhánh:** `feature/phase-02b2a-write-contract`

Ba lát trước dựng xong câu hỏi "ai gọi, được làm gì". Lát này trả lời phần còn
lại của một lệnh ghi: **gửi lại thì sao**, **hai người cùng sửa thì sao**, và
**cấu hình lấy ở đâu**.

## Thứ tự idempotency: một dự định đúng về mặt bất biến, sai về mặt tình huống

Kế hoạch ban đầu là *làm việc trước, ghi khóa sau*, cả hai cùng transaction. Lý
lẽ nghe vững: ghi khóa ở transaction riêng sẽ để lại khóa trỏ tới chứng từ chưa
từng tồn tại — đúng điều RT-12 cấm.

Test bốn luồng cùng một khóa cho thấy nó hỏng ở đúng tình huống nó sinh ra để
phục vụ. Lần gửi lại mang **nguyên** nội dung cũ, nên nó đụng ràng buộc duy nhất
của *bảng nghiệp vụ* — mã chi nhánh hôm nay, số chứng từ ở phase 6 — **trước**
khi chạm tới bảng khóa. Người gọi nhận một lỗi ràng buộc thô thay vì kết quả của
lần thực hiện trước.

Sửa: giành khóa (`INSERT` + `flush`) trước, rồi làm việc, rồi điền kết quả — vẫn
một transaction. Ràng buộc duy nhất của bảng khóa trở thành **cửa vào**, và
PostgreSQL cho request thứ hai *chờ* thay vì lỗi ngay, nên không có nhánh "cả
hai cùng thấy trống rồi cùng ghi".

Bài học không phải "viết test trước". Bài học là một bất biến đúng
(*cùng transaction*) không tự nói cho ta biết **thứ tự** nào bên trong nó là
đúng — chỉ tình huống đồng thời thật mới nói.

## Cổng kiểm tra suýt xanh vì không thấy gì

Quy ước "mọi POST đổi trạng thái phải có `X-Idempotency-Key`" là loại quy ước sẽ
mục dần từ phase 6, nên nó được ép bằng một test duyệt bảng route thật.

Test đó xanh ngay lần chạy đầu — và xanh **khống**: FastAPI từ 0.14x không trải
phẳng router con vào `app.routes` nữa, nên vòng lặp chỉ thấy `/health`. Một cổng
kiểm tra không thấy gì thì không canh gì, và nó không có cách nào tự báo.

Nay `iter_api_routes` duyệt cả hai kiểu bọc, và chính test có một khẳng định đối
chứng: danh sách route POST rỗng là **lỗi**, không phải là "không có vi phạm".
Mọi cổng chống-mục-nát nên có một dòng như vậy.

## Review thù địch: chỗ hỏng không nằm ở phần khó

Lõi idempotency — phần tốn nhiều suy nghĩ nhất — đứng vững: 24/32 đột biến bị
bắt, mọi đột biến vào bất biến "cùng transaction" đều đỏ. Ba lỗ nghiêm trọng
nằm ở những chỗ tưởng là phụ:

**Hạn mức request tin vào dữ liệu của kẻ tấn công.** Định danh ngân sách lấy từ
băm header `Authorization` — *chưa xác thực*. Gửi `Bearer rac-1`, `rac-2`, … là
mỗi request một ngân sách mới. Đo được: 50 lần thử đăng nhập, không lần nào bị
chặn. Middleware tuyên bố ngay ở dòng đầu docstring rằng nó chặn dò mật khẩu
trong LAN, và nó không chặn được gì cả. Kèm theo: bơm định danh giả đẩy bảng qua
trần → `clear()` xóa luôn bộ đếm người thật, tức là biến chính cơ chế bảo vệ bộ
nhớ thành **công tắc tắt hạn mức**.

**Khóa lạc quan ở tùy chọn so nhầm dòng.** Phản hồi trả `row_version` của *dòng
đang hiệu lực*, còn lúc ghi thì so với dòng của *cấp được yêu cầu*. Hai dòng
khác nhau, nên phép kiểm vô nghĩa theo cả hai chiều: người dùng đã có giá trị
chung thì **không đặt được** giá trị riêng, còn người có giá trị riêng thì **ghi
đè im lặng** được dòng chung mình chưa từng nhìn thấy. Cả hai đều là hỏng đúng
vào tính năng mà FR-SYS-060 và FR-NFR-005 mô tả.

Điểm chung của cả hai: chúng có test, và test xanh — vì test viết theo cách mã
đang chạy, không theo cách người dùng thật sẽ gọi. Test tùy chọn gửi
`row_version: null` cứng, nên nó không bao giờ đi vào con đường mà client thật
phải đi.

**Mã chi nhánh trùng trả `500`.** Lỗi gõ tay thường gặp nhất của người nhập liệu
nhận về "đã xảy ra lỗi không mong muốn, cung cấp mã tham chiếu cho bộ phận hỗ
trợ". Không có gì trong thiết kế sai — chỉ là không tầng nào nhận trách nhiệm
dịch `IntegrityError` sang ngôn ngữ người dùng, và endpoint đầu tiên chạm vào nó
là endpoint đầu tiên trong repo có ràng buộc duy nhất trên đường ghi.

## Hai thứ nhỏ đáng nhớ

`SET LOCAL lock_timeout` — thiết kế idempotency **cố ý** dựa vào việc request
trùng khóa chờ. Nhưng "chờ" mặc định của PostgreSQL là *vô hạn*, và pool chỉ có
15 chỗ. Một cơ chế đúng có thể trở thành sự cố sẵn sàng chỉ vì một giá trị mặc
định không ai nghĩ tới.

Bộ quét cấm `float` bắt được `time.monotonic()` trong middleware hạn mức. Ở đó
không có đồng tiền nào để làm tròn sai, và thêm một ngoại lệ vào hàng rào là
việc mười giây. Đổi sang `monotonic_ns()` cũng mười giây — nhưng hàng rào còn
nguyên cho lần sau, khi chỗ dùng `float` thật sự nằm trên đường tính tiền.
