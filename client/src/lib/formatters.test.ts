/**
 * Định dạng để **hiển thị** — và bất biến "không có phép tính tiền ở client".
 *
 * Số tiền đi từ server dưới dạng **chuỗi** và phải ở dạng chuỗi tới tận
 * `Intl.NumberFormat`. Chuyển qua `number` là chỗ mất chính xác đầu tiên, và
 * với cột tiền 15 chữ số thì đó không phải chuyện lý thuyết.
 */

import { describe, expect, it } from 'vitest'

import { formatDate, formatMoney } from '@/lib/formatters'

describe('formatMoney', () => {
  it('giữ nguyên mọi chữ số của số vượt quá độ chính xác của `number`', () => {
    // Qua `Number()`, giá trị này thành 12345678901234568 — sai từ chữ số thứ 17.
    const formatted = formatMoney('12345678901234567.89', 'vi')

    expect(formatted).toContain('567')
    expect(formatted).not.toContain('568')
  })

  it('giữ đúng số chữ số thập phân mà server gửi', () => {
    expect(formatMoney('1000', 'vi')).not.toContain(',')
    expect(formatMoney('1000.50', 'vi')).toContain(',50')
  })
})

describe('formatDate', () => {
  it('chuỗi không phải ngày thì trả nguyên văn thay vì "Invalid Date"', () => {
    expect(formatDate('chưa có', 'vi')).toBe('chưa có')
  })
})
