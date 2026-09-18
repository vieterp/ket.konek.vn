/**
 * Tra bảng quyết định: bước 2 hỏi hay không đọc từ bảng; dòng khớp theo cả hai
 * câu trả lời; thiếu câu hai khi bảng đòi → chưa khớp (không rơi về nhánh nào).
 */

import { describe, expect, it } from 'vitest'

import { asksBuyerDeclared, matchFlow, type ErrorFlowOut } from './error-flow-match'
import { ERROR_FLOWS } from './feature-test-utils'

const flows = ERROR_FLOWS.body as unknown as readonly ErrorFlowOut[]

describe('matchFlow', () => {
  it('hủy không hỏi câu hai và khớp ngay', () => {
    expect(asksBuyerDeclared(flows, 2)).toBe(false)
    expect(matchFlow(flows, 2, null)?.remedy).toBe(3)
  })

  it('sai tiền hỏi câu hai; thiếu thì chưa khớp, có thì ra đúng nhánh', () => {
    expect(asksBuyerDeclared(flows, 1)).toBe(true)
    expect(matchFlow(flows, 1, null)).toBeNull()
    expect(matchFlow(flows, 1, false)?.remedy).toBe(0)
    expect(matchFlow(flows, 1, true)?.remedy).toBe(2)
  })

  it('chưa chọn loại sai sót → null', () => {
    expect(matchFlow(flows, null, true)).toBeNull()
  })
})
