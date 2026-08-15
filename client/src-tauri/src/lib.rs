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
//! Phase 1 chưa khai lệnh (`#[tauri::command]`) nào.

/// Khởi động shell.
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .run(tauri::generate_context!())
        .expect("không khởi động được Tauri shell");
}
