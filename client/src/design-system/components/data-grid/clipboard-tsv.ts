/**
 * Đọc vùng ô dán từ bảng tính (Excel, Google Sheets, LibreOffice).
 *
 * Cả ba đều đặt lên clipboard một bản `text/plain` dạng TSV: ô cách nhau bằng
 * `\t`, dòng cách nhau bằng `\r\n` (Windows) hoặc `\n`. Nghe như `split` là
 * xong — và đó chính là chỗ hỏng.
 *
 * Ô nào **chứa** tab, xuống dòng hoặc dấu nháy kép thì bảng tính bọc nó trong
 * `"…"` và nhân đôi dấu nháy bên trong. Với phần mềm kế toán đây không phải ca
 * hiếm: địa chỉ đối tác ("123 Lê Lợi, P.2\nQ.Bình Thạnh") và diễn giải chứng từ
 * xuống dòng là chuyện thường. `text.split('\n')` cắt đúng giữa một ô như thế
 * và làm lệch **toàn bộ** phần còn lại của vùng dán — 200 dòng số tiền tụt đi
 * một hàng, không có gì trên màn hình báo cho người dùng biết.
 *
 * Nên phải quét theo ký tự với một máy trạng thái. Vẫn là một lượt duy nhất qua
 * chuỗi, nên chi phí không đáng kể so với ngưỡng "dán 200 dòng < 1s".
 *
 * Hàm này chỉ **tách chuỗi**. Không ép kiểu, không bỏ dấu phân cách nghìn,
 * không đổi "1.234,50" thành số — mọi việc đó là của server (LD-03, quyết định
 * H15). Ô nào sai định dạng thì phải nhìn thấy nguyên văn thứ mình vừa dán để
 * còn biết đường sửa.
 */

/**
 * Tách nội dung clipboard thành ma trận ô.
 *
 * Trả mảng rỗng khi chuỗi rỗng. Dòng cuối rỗng do bảng tính kết thúc vùng chọn
 * bằng một dấu xuống dòng bị loại — nếu không, dán 200 dòng luôn ghi đè thêm
 * một dòng thứ 201 toàn ô trắng.
 */
export function parseClipboardTable(text: string): readonly (readonly string[])[] {
  if (text === '') {
    return []
  }

  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  let index = 0

  while (index < text.length) {
    const char = text.charAt(index)

    if (quoted) {
      if (char === '"') {
        // `""` bên trong ô đã bọc = một dấu nháy thật.
        if (text.charAt(index + 1) === '"') {
          cell += '"'
          index += 2
          continue
        }
        quoted = false
        index += 1
        continue
      }
      cell += char
      index += 1
      continue
    }

    // Chỉ mở ngoặc khi ô đang rỗng: `A"B` là ba ký tự thật, không phải ô bọc.
    if (char === '"' && cell === '') {
      quoted = true
      index += 1
      continue
    }

    if (char === '\t') {
      row.push(cell)
      cell = ''
      index += 1
      continue
    }

    if (char === '\n' || char === '\r') {
      row.push(cell)
      cell = ''
      rows.push(row)
      row = []
      index += char === '\r' && text.charAt(index + 1) === '\n' ? 2 : 1
      continue
    }

    cell += char
    index += 1
  }

  row.push(cell)
  rows.push(row)

  // Vùng chọn của bảng tính kết thúc bằng một dấu xuống dòng → dòng cuối là
  // một ô rỗng duy nhất. Chỉ bỏ khi còn dòng khác: dán đúng một ô trống là
  // thao tác hợp lệ (xóa nội dung ô).
  const last = rows[rows.length - 1]
  if (rows.length > 1 && last !== undefined && last.length === 1 && last[0] === '') {
    rows.pop()
  }

  return rows
}
