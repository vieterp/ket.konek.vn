/**
 * `useDimensionLookups.resolveMissingCodes` — đường rà mã chiều LÚC CẤT.
 *
 * Lát 6G-2 gộp `useSlugLookup` của nhóm này vào `useMasterSearchLookup` của
 * nhóm 03; review sau đó cho thấy đột biến "bỏ lời gọi `fetchCodes`" **sống
 * sót** vì không bài kiểm nào chạm tới đường này — nó chỉ chạy khi người dùng
 * gõ một mã nằm ngoài trang seed rồi bấm Cất, và ba form đều đi qua nó.
 *
 * Kiểm ở tầng hook chứ không lái cả form qua lưới nhập liệu: thứ cần chứng
 * minh là "mã ngoài trang seed có được tra `search=` và gộp vào bản đồ trả về
 * hay không", và lưới chỉ làm câu hỏi ấy đắt hơn để hỏi.
 */

import type { ReactElement } from 'react'
import { useEffect, useState } from 'react'
import { screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  EMPTY_LIST,
  baseRoutes,
  mockServer,
  renderWithSession,
  seedSession,
  type FakeRoutes,
} from './feature-test-utils'
import { useDimensionLookups } from './use-dimension-lookups'

const LATE_ROW = {
  id: 42,
  uid: '019-cong-trinh-moi',
  code: 'CT-MOI',
  name: 'Công trình mới lập',
  name_en: null,
  parent_id: null,
  path: '42',
  level: 0,
  is_group: false,
  is_active: true,
  branch_id: null,
  row_version: 1,
}

function Probe(): ReactElement {
  const lookups = useDimensionLookups()
  const [resolved, setResolved] = useState<string>('')

  useEffect(() => {
    if (lookups.isLoading) {
      return
    }
    void lookups
      .resolveMissingCodes([{ slug: 'projects', code: 'CT-MOI' }])
      .then((options) => {
        const found = (options.projects ?? []).find((item) => item.code === 'CT-MOI')
        setResolved(found === undefined ? 'KHONG-TIM-THAY' : `TIM-THAY:${String(found.id)}`)
      })
    // Chạy đúng một lần sau khi seed xong — `lookups` đổi tham chiếu mỗi render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lookups.isLoading])

  return <div data-testid="ket-qua">{resolved}</div>
}

function routes(): FakeRoutes {
  return {
    ...baseRoutes(),
    '/master/partners': EMPTY_LIST,
    '/master/employees': EMPTY_LIST,
    '/master/cost_objects': EMPTY_LIST,
    '/master/contracts': EMPTY_LIST,
    '/master/expense_items': EMPTY_LIST,
    '/master/items': EMPTY_LIST,
    '/master/warehouses': EMPTY_LIST,
    '/master/company_bank_accounts': EMPTY_LIST,
    // Trang seed TRỐNG; mã chỉ tra được qua `search=` — đúng hình dạng "danh
    // mục lớn, bản ghi mới lập nằm ngoài trang đầu".
    '/master/projects': (_init: RequestInit | undefined, url?: string) =>
      url !== undefined && url.includes('search=CT-MOI')
        ? { status: 200, body: { items: [LATE_ROW], total: 1 } }
        : { status: 200, body: { items: [], total: 0 } },
  }
}

describe('useDimensionLookups.resolveMissingCodes', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('tra mã ngoài trang seed bằng search= rồi trả bản đồ ĐÃ gộp', async () => {
    const fetchMock = mockServer(routes())
    renderWithSession(<Probe />)

    await waitFor(() => {
      expect(screen.getByTestId('ket-qua')).toHaveTextContent('TIM-THAY:42')
    })
    // Bản đồ trả THẲNG cho lượt rà thứ hai — lượt gọi hiện tại không chờ được
    // vòng render sau, nên "đã gộp vào state" một mình là chưa đủ.
    expect(
      fetchMock.mock.calls.some((entry) => String(entry[0]).includes('search=CT-MOI')),
    ).toBe(true)
  })
})
