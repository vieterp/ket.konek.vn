/**
 * Câu xóa vướng tờ HĐĐT nháp: chỉ nhận đúng tên ràng buộc của khóa ngoại
 * `einvoices.source_voucher_id`; mọi lỗi khác trả `null` để câu chung xử tiếp.
 */

import { describe, expect, it } from 'vitest'

import { vi } from '@/locales/vi'
import type { Translate } from '@/lib/i18n'

import { deleteBlockedByDraftEInvoice, EINVOICE_SOURCE_CONSTRAINT } from './delete-blocked-message'

const t: Translate = (key) => vi[key]

function problem(details: Record<string, string | number | null> | null): Parameters<typeof deleteBlockedByDraftEInvoice>[1] {
  return {
    type: 'https://konek.vn/errors/data.constraint_violation',
    title: 'data.constraint_violation',
    status: 422,
    detail: 'Dữ liệu vi phạm ràng buộc của cơ sở dữ liệu',
    error_code: 'data.constraint_violation',
    details,
  }
}

describe('deleteBlockedByDraftEInvoice', () => {
  it('ràng buộc tờ HĐĐT → câu nói bước phải làm + liên kết mang source_voucher_id', () => {
    const message = deleteBlockedByDraftEInvoice(t, problem({ constraint: EINVOICE_SOURCE_CONSTRAINT }), 'v-1')
    expect(message).toEqual({
      text: 'Chứng từ còn tờ hóa đơn điện tử nháp. Hủy tờ nháp ở Hóa đơn điện tử trước, rồi xóa chứng từ.',
      linkLabel: 'Mở hóa đơn điện tử của chứng từ',
      href: '/hoa-don-dien-tu?source_voucher_id=v-1',
    })
  })

  it('ràng buộc khác hoặc không có details → null', () => {
    expect(deleteBlockedByDraftEInvoice(t, problem({ constraint: 'fk_khac' }), 'v-1')).toBeNull()
    expect(deleteBlockedByDraftEInvoice(t, problem(null), 'v-1')).toBeNull()
  })
})
