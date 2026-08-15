// Ẩn cửa sổ console trên Windows ở bản phát hành.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    ket_lib::run()
}
