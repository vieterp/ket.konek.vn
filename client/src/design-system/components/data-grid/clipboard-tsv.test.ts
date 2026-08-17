/**
 * Quy tắc TSV của bảng tính.
 *
 * Mỗi ca dưới đây là một cách `text.split('\t')` làm lệch cả vùng dán mà không
 * báo gì — với 200 dòng số tiền thì lệch một hàng là một chứng từ sai.
 */

import { describe, expect, it } from 'vitest'

import { parseClipboardTable } from './clipboard-tsv'

describe('parseClipboardTable', () => {
  it('tách ô theo tab và dòng theo xuống dòng', () => {
    expect(parseClipboardTable('a\tb\nc\td')).toEqual([
      ['a', 'b'],
      ['c', 'd'],
    ])
  })

  it('đọc được xuống dòng kiểu Windows lẫn Unix', () => {
    expect(parseClipboardTable('a\r\nb\nc\rd')).toEqual([['a'], ['b'], ['c'], ['d']])
  })

  it('ô bọc trong ngoặc kép giữ nguyên tab và xuống dòng bên trong', () => {
    // Ca thật: địa chỉ đối tác nhiều dòng, diễn giải chứng từ có xuống dòng.
    const clipboard = '"123 Lê Lợi\nP.2, Q.Bình Thạnh"\t5000000\nCông ty A\t250000'

    expect(parseClipboardTable(clipboard)).toEqual([
      ['123 Lê Lợi\nP.2, Q.Bình Thạnh', '5000000'],
      ['Công ty A', '250000'],
    ])
  })

  it('`""` bên trong ô bọc là một dấu nháy thật', () => {
    expect(parseClipboardTable('"Máy in ""Canon"" LBP"\t1')).toEqual([['Máy in "Canon" LBP', '1']])
  })

  it('dấu nháy giữa ô là ký tự thường, không mở vùng bọc', () => {
    expect(parseClipboardTable('15" màn hình\t2')).toEqual([['15" màn hình', '2']])
  })

  it('bỏ dòng rỗng cuối do bảng tính thêm vào', () => {
    expect(parseClipboardTable('a\tb\n')).toEqual([['a', 'b']])
  })

  it('giữ ô trống ở giữa — người dùng cố ý xóa nội dung ô đó', () => {
    expect(parseClipboardTable('a\t\tc')).toEqual([['a', '', 'c']])
    expect(parseClipboardTable('')).toEqual([])
  })

  it('không đụng vào định dạng số — đó là việc của server', () => {
    // Ép kiểu ở client là đường ngắn nhất tới một con số sai trên BCTC (H15).
    expect(parseClipboardTable('1.234,50\t1,234.50')).toEqual([['1.234,50', '1,234.50']])
  })

  it('dán 200 dòng × 6 cột ra đủ 1.200 ô', () => {
    const clipboard = Array.from({ length: 200 }, (_, index) =>
      Array.from({ length: 6 }, (_, cell) => `${index}-${cell}`).join('\t'),
    ).join('\n')

    const table = parseClipboardTable(clipboard)

    expect(table).toHaveLength(200)
    expect(table[199]).toEqual(['199-0', '199-1', '199-2', '199-3', '199-4', '199-5'])
  })
})
