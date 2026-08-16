/**
 * Chuỗi giao diện tiếng Việt — **nguồn khóa** của toàn bộ ứng dụng (FR-NFR-034).
 *
 * Khóa phẳng, chấm phân cấp theo màn hình. Không lồng object: khóa phẳng tra
 * được bằng `grep` từ chính chuỗi thấy trên màn hình, và đó là thao tác hay
 * phải làm nhất khi sửa một câu chữ.
 *
 * Tiếng Việt là bản gốc chứ không phải bản dịch: sản phẩm này viết cho kế toán
 * viên Việt Nam, và thuật ngữ nghiệp vụ (chứng từ, ghi sổ, dữ liệu kế toán)
 * không có bản tiếng Anh nào chuẩn hơn để dịch ngược lại.
 *
 * `en.ts` khai **đúng** bộ khóa này — thiếu hay thừa một khóa là lỗi `tsc`,
 * không phải một chuỗi lạ hiện trên màn hình khách hàng (quyết định H4).
 */

export const vi = {
  'common.appName': 'Konek Két',
  'common.tagline': 'Phần mềm kế toán',
  'common.loading': 'Đang tải…',
  'common.retry': 'Thử lại',
  'common.cancel': 'Hủy',
  'common.close': 'Đóng',
  'common.signOut': 'Đăng xuất',
  'common.language': 'Ngôn ngữ',
  'common.vietnamese': 'Tiếng Việt',
  'common.english': 'English',
  'common.version': 'Phiên bản {version}',
  'common.required': 'Bắt buộc',

  'login.title': 'Đăng nhập',
  'login.subtitle': 'Nhập tài khoản được cấp để vào sổ sách của doanh nghiệp.',
  'login.username': 'Tên đăng nhập',
  'login.password': 'Mật khẩu',
  'login.totpCode': 'Mã xác thực hai lớp',
  'login.totpHint': 'Mở ứng dụng xác thực trên điện thoại và nhập 6 chữ số đang hiện.',
  'login.submit': 'Đăng nhập',
  'login.submitting': 'Đang đăng nhập…',
  'login.serverLabel': 'Máy chủ',

  'passwordChange.title': 'Đổi mật khẩu',
  'passwordChange.intro':
    'Tài khoản đang dùng mật khẩu tạm. Đặt mật khẩu mới trước khi vào phần nghiệp vụ.',
  'passwordChange.currentPassword': 'Mật khẩu hiện tại',
  'passwordChange.newPassword': 'Mật khẩu mới',
  'passwordChange.confirmPassword': 'Nhập lại mật khẩu mới',
  'passwordChange.mismatch': 'Hai lần nhập mật khẩu mới không giống nhau.',
  'passwordChange.submit': 'Đổi mật khẩu',
  'passwordChange.otherSessionsWarning':
    'Đổi xong, mọi phiên đăng nhập khác của tài khoản này sẽ bị thoát.',

  'totp.title': 'Đăng ký xác thực hai lớp',
  'totp.intro':
    'Vai trò của tài khoản này bắt buộc xác thực hai lớp. Đăng ký một thiết bị sinh mã rồi đăng nhập lại.',
  'totp.password': 'Nhập lại mật khẩu để bắt đầu',
  'totp.begin': 'Bắt đầu đăng ký',
  'totp.scan': 'Quét mã QR bằng ứng dụng xác thực (Google Authenticator, Microsoft Authenticator…).',
  'totp.cantScan': 'Không quét được? Nhập tay chuỗi bí mật sau vào ứng dụng:',
  'totp.code': 'Mã 6 chữ số đang hiện',
  'totp.confirm': 'Xác nhận thiết bị',
  'totp.confirmed': 'Đã đăng ký thiết bị. Đăng nhập lại và nhập mã để vào ứng dụng.',
  'totp.qrAlt': 'Mã QR đăng ký thiết bị xác thực',

  'update.title': 'Cần cập nhật ứng dụng',
  'update.body':
    'Bản đang chạy trên máy này cũ hơn bản tối thiểu mà máy chủ chấp nhận. Tra cứu sổ sách vẫn dùng được, nhưng mọi thao tác lưu đều bị từ chối cho tới khi cập nhật.',
  'update.currentVersion': 'Bản đang chạy: {version}',
  'update.requiredVersion': 'Bản tối thiểu: {version}',
  'update.howTo': 'Ứng dụng sẽ tự cập nhật khi khởi động lại. Không được thì báo người quản trị.',
  'update.continueReadOnly': 'Tiếp tục ở chế độ chỉ đọc',

  'handshake.serverBehind':
    'Bản trên máy này ({client}) mới hơn máy chủ ({server}). Vẫn làm việc được, nhưng nên cập nhật máy chủ.',
  'handshake.failed': 'Không kết nối được máy chủ {url}.',

  'dataset.title': 'Chọn dữ liệu kế toán',
  'dataset.intro': 'Mỗi doanh nghiệp là một bộ sổ độc lập. Chọn bộ sổ muốn làm việc.',
  'dataset.empty': 'Tài khoản này chưa được gán dữ liệu kế toán nào. Liên hệ người quản trị.',
  'dataset.switch': 'Đổi dữ liệu kế toán',

  'nav.home': 'Tổng quan',
  'nav.tien-vao-tien-ra': 'Tiền vào tiền ra',
  'nav.mua-hang': 'Mua hàng',
  'nav.ban-hang': 'Bán hàng',
  'nav.hoa-don-dien-tu': 'Hóa đơn điện tử',
  'nav.kho': 'Kho',
  'nav.tai-san': 'Tài sản',
  'nav.luong': 'Lương',
  'nav.so-sach-thue': 'Sổ sách & Thuế',
  'nav.danh-muc-thiet-lap': 'Danh mục & Thiết lập',

  'theme.label': 'Chế độ hiển thị',
  'theme.system': 'Theo hệ thống',
  'theme.light': 'Sáng',
  'theme.dark': 'Tối',

  'status.branch': 'Chi nhánh',
  'status.allBranches': 'Tất cả chi nhánh được gán',
  'status.noBranch': 'Chưa gán chi nhánh',
  'status.readOnly': 'Chế độ chỉ đọc',
  'status.dataset': 'Dữ liệu kế toán',

  'placeholder.title': 'Nhóm màn hình {group}',
  'placeholder.body': 'Màn hình nghiệp vụ của nhóm này thuộc phase sau của kế hoạch.',

  'error.auth.invalid_credentials': 'Tên đăng nhập hoặc mật khẩu không đúng.',
  'error.auth.account_locked': 'Tài khoản đang bị khóa do nhập sai nhiều lần. Thử lại sau ít phút.',
  'error.auth.throttled': 'Quá nhiều lần thử. Chờ một lát rồi thử lại.',
  'error.auth.totp_code_invalid': 'Mã xác thực không đúng.',
  'error.auth.totp_code_reused': 'Mã này vừa được dùng. Chờ mã tiếp theo trên ứng dụng xác thực.',
  'error.auth.password_too_weak': 'Mật khẩu mới chưa đạt yêu cầu về độ mạnh.',
  'error.auth.not_authenticated': 'Phiên đăng nhập đã hết hạn. Đăng nhập lại.',
  'error.auth.permission_denied': 'Tài khoản không có quyền thực hiện việc này.',
  'error.dataset.access_denied': 'Tài khoản không có quyền trong dữ liệu kế toán này.',
  'error.system.client_version_unsupported': 'Bản client này quá cũ để lưu dữ liệu.',
  'error.system.app_key_unavailable':
    'Máy chủ chưa cấu hình khóa mã hóa. Báo người quản trị hệ thống.',
  'error.request.validation_failed': 'Dữ liệu nhập chưa hợp lệ.',
  'error.transport.unexpected_response': 'Máy chủ trả lời không đọc được.',
  'error.transport.unreachable': 'Không kết nối được máy chủ.',
  'error.unknown': 'Đã xảy ra lỗi ({code}). Cung cấp mã tham chiếu cho bộ phận hỗ trợ.',
} as const

export type TranslationKey = keyof typeof vi
