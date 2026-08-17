//! Desktop shell của Konek Két.
//!
//! Ranh giới kiến trúc (luật phụ thuộc #6, docs/system-architecture.md):
//! shell **không** tham gia luồng nghiệp vụ. Nó chỉ cung cấp bốn thứ mà trình
//! duyệt không làm được: tự cập nhật, in ấn, chọn/lưu tệp, giữ phiên. Mọi
//! nghiệp vụ đi qua REST tới app server, nên cùng bundle web này chạy được
//! trong trình duyệt LAN ở v1.x mà không mất chức năng nghiệp vụ nào.
//!
//! Ký số USB token **không** ở đây — đó là dịch vụ esign riêng (ADR-016).
//!
//! Hai lệnh của lát 2C-4 (`check_and_install_update`, `restart_app`) là toàn bộ
//! phần Rust của đường tự cập nhật; xem docstring của chúng.

use tauri::{AppHandle, Url};
use tauri_plugin_updater::UpdaterExt;

/// Khuôn endpoint kiểm cập nhật. Ba chỗ trong ngoặc do **Tauri** thay lúc chạy.
///
/// Giữ ở đây chứ không trong `tauri.conf.json`: xem `check_and_install_update`.
const UPDATE_PATH: &str = "/updates/{{target}}/{{arch}}/{{current_version}}";

/// Kiểm và cài bản cập nhật từ **chính app server người dùng đang dùng**.
///
/// Trả `true` nếu đã cài xong một bản mới (khi ấy phải khởi động lại), `false`
/// nếu không có bản nào mới hơn.
///
/// # Vì sao địa chỉ đến từ JavaScript
///
/// `tauri.conf.json` cho phép ghim endpoint lúc dựng, và bản đầu của cấu hình
/// này đã ghim `https://localhost:5443`. Đó là một lỗi: ở chế độ LAN (LD-01)
/// app server nằm trên **máy khác**, nên mỗi máy trạm sẽ đi hỏi chính nó xem có
/// bản mới không — updater im lặng không bao giờ tìm thấy gì. Ghim host cũng
/// nghĩa là mỗi khách hàng cần một installer riêng.
///
/// Địa chỉ vì thế phải là địa chỉ máy chủ mà người dùng đã cấu hình, và tầng
/// duy nhất biết nó là web UI.
///
/// # Vì sao nhận địa chỉ từ JavaScript vẫn an toàn
///
/// Vì **khóa công khai ghim trong `tauri.conf.json` mới là thứ quyết định**. Gói
/// cập nhật phải mang chữ ký khớp khóa đó thì updater mới cài; một webview bị
/// chiếm chỉ trỏ được updater tới một máy chủ không sinh nổi chữ ký hợp lệ, và
/// bản cập nhật sẽ bị từ chối ở bước xác minh.
///
/// Hệ quả phải giữ: **khóa ký không bao giờ được cấu hình lúc chạy.** Ngày nào
/// khóa cũng đi vào từ JavaScript thì cả cơ chế này rỗng.
///
/// Vẫn chặn sơ đồ lạ (`file:`, `data:`) để một URL dị dạng không thành một
/// đường đọc tệp cục bộ.
#[tauri::command]
async fn check_and_install_update(app: AppHandle, server_base_url: String) -> Result<bool, String> {
    let endpoint = build_endpoint(&server_base_url)?;

    let updater = app
        .updater_builder()
        .endpoints(vec![endpoint])
        .map_err(|error| format!("không đặt được địa chỉ máy chủ cập nhật: {error}"))?
        .build()
        .map_err(|error| format!("không dựng được updater: {error}"))?;

    let Some(update) = updater
        .check()
        .await
        .map_err(|error| format!("không hỏi được máy chủ cập nhật: {error}"))?
    else {
        return Ok(false);
    };

    update
        .download_and_install(|_downloaded, _total| {}, || {})
        .await
        .map_err(|error| format!("tải hoặc cài bản cập nhật không thành công: {error}"))?;

    Ok(true)
}

/// Dựng URL kiểm cập nhật từ địa chỉ máy chủ, và từ chối sơ đồ không phải HTTP.
fn build_endpoint(server_base_url: &str) -> Result<Url, String> {
    let base = server_base_url.trim_end_matches('/');
    let url = Url::parse(&format!("{base}{UPDATE_PATH}"))
        .map_err(|error| format!("địa chỉ máy chủ không hợp lệ: {error}"))?;
    if !matches!(url.scheme(), "http" | "https") {
        return Err(format!(
            "địa chỉ máy chủ phải là http hoặc https, nhận {}",
            url.scheme()
        ));
    }
    Ok(url)
}

/// Khởi động lại ứng dụng sau khi đã cài xong bản mới.
///
/// Lệnh **riêng** thay vì khởi động lại ngay trong `check_and_install_update`:
/// tiến trình thoát thì Promise phía JavaScript không bao giờ hoàn tất, nên màn
/// hình không kịp báo "đã cập nhật xong". Tách ra thì người dùng đọc được kết
/// quả rồi tự bấm — và người đang gõ dở một chứng từ không bị đá ra giữa chừng.
#[tauri::command]
fn restart_app(app: AppHandle) -> Result<(), String> {
    app.restart();
}

/// Khởi động shell.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            check_and_install_update,
            restart_app
        ])
        .run(tauri::generate_context!())
        .expect("không khởi động được Tauri shell");
}

#[cfg(test)]
mod tests {
    use super::build_endpoint;

    /// Chỗ giữ chỗ nằm trong URL ở dạng **đã mã hóa phần trăm**, và đó là bình
    /// thường: `Url::parse` mã hóa `{` `}`, còn `tauri-plugin-updater` thay cả
    /// hai dạng — nó gọi `.replace("%7B%7Btarget%7D%7D", …)` trước rồi mới
    /// `.replace("{{target}}", …)` (updater.rs §check).
    ///
    /// Khẳng định đúng chuỗi đã mã hóa chứ không "đại khái có target": nếu một
    /// ngày nào đó cách dựng URL đổi làm chuỗi này khác đi, updater sẽ gọi tới
    /// một đường dẫn có `%7B%7B` nguyên xi, server trả `204`, và triệu chứng duy
    /// nhất là **không máy nào cập nhật được** — không lỗi, không log phía client.
    #[test]
    fn builds_endpoint_from_the_configured_server() {
        let url = build_endpoint("https://ketserver.noi-bo:5443").unwrap();
        assert_eq!(
            url.as_str(),
            "https://ketserver.noi-bo:5443/updates/%7B%7Btarget%7D%7D/%7B%7Barch%7D%7D/%7B%7Bcurrent_version%7D%7D"
        );
    }

    #[test]
    fn trailing_slash_does_not_double_up() {
        let with_slash = build_endpoint("https://host:5443/").unwrap();
        let without = build_endpoint("https://host:5443").unwrap();
        assert_eq!(with_slash, without);
    }

    #[test]
    fn refuses_non_http_schemes() {
        // Một URL dị dạng không được biến thành đường đọc tệp cục bộ.
        assert!(build_endpoint("file:///etc").is_err());
        assert!(build_endpoint("khong-phai-url").is_err());
    }
}
