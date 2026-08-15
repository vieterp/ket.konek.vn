// Ẩn cửa sổ console trên Windows ở bản phát hành.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    konek_accounting_lib::run()
}
