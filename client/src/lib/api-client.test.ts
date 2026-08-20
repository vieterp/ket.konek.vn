/**
 * Hợp đồng HTTP mà mọi màn hình dựa vào.
 *
 * Bốn header và một hình dạng lỗi. Chúng không có gì "thông minh", nhưng mỗi
 * cái quên gửi lại hỏng theo một kiểu khó lần: thiếu `X-Client-Version` thì mọi
 * lệnh ghi trả `426` (H2); thiếu `X-Dataset` thì server trả
 * `dataset.header_missing`; gửi sai `Authorization` thì người dùng bị đá ra
 * giữa chừng.
 */

import type { Mock } from 'vitest'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiClient, ApiError, newIdempotencyKey } from '@/lib/api-client'
import { APP_VERSION } from '@/lib/app-version'

const BASE = 'http://ket.test'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function problemResponse(errorCode: string, status: number): Response {
  return new Response(
    JSON.stringify({
      type: `https://konek.vn/errors/${errorCode}`,
      title: errorCode,
      status,
      detail: 'câu dành cho người vận hành',
      error_code: errorCode,
    }),
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

function headersOf(fetchMock: Mock<typeof fetch>): Record<string, string> {
  const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
  return init.headers as Record<string, string>
}

describe('ApiClient', () => {
  let fetchMock: Mock<typeof fetch>

  beforeEach(() => {
    fetchMock = vi.fn<typeof fetch>()
    vi.stubGlobal('fetch', fetchMock)
  })

  it('khai phiên bản client ở mọi request, kể cả lệnh đọc', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: 'ok' }))
    const client = new ApiClient({ baseUrl: BASE })

    await client.get('/health')

    // Gắn có điều kiện nghĩa là thêm một nhánh để hỏng, và log của server mất
    // thông tin "máy trạm nào đang chạy bản nào" đúng lúc cần nó nhất.
    expect(headersOf(fetchMock)['X-Client-Version']).toBe(APP_VERSION)
  })

  it('chỉ gửi Authorization sau khi có token', async () => {
    // Mỗi lần gọi phải là một `Response` MỚI: thân của `Response` chỉ đọc
    // được một lần, nên `mockResolvedValue` (một đối tượng dùng lại) sẽ hỏng ở
    // lần gọi thứ hai với lỗi không liên quan gì tới thứ đang kiểm.
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})))
    const client = new ApiClient({ baseUrl: BASE })

    await client.get('/api/v1/auth/me')
    expect(headersOf(fetchMock).Authorization).toBeUndefined()

    fetchMock.mockClear()
    client.setToken('token-abc')
    await client.get('/api/v1/auth/me')
    expect(headersOf(fetchMock).Authorization).toBe('Bearer token-abc')
  })

  it('gửi X-Dataset và X-Branch khi chỗ gọi khai', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}))
    const client = new ApiClient({ baseUrl: BASE })

    await client.get('/api/v1/system/access', { datasetCode: 'alpha', branchId: 3 })

    const headers = headersOf(fetchMock)
    expect(headers['X-Dataset']).toBe('alpha')
    expect(headers['X-Branch']).toBe('3')
  })

  it('không tự sinh khóa idempotency', async () => {
    // Mỗi lần gọi phải là một `Response` MỚI: thân của `Response` chỉ đọc
    // được một lần, nên `mockResolvedValue` (một đối tượng dùng lại) sẽ hỏng ở
    // lần gọi thứ hai với lỗi không liên quan gì tới thứ đang kiểm.
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})))
    const client = new ApiClient({ baseUrl: BASE })

    await client.post('/api/v1/system/branches', { code: 'CN1' })

    // Sinh tự động mỗi lần gọi sẽ **phá** chính cơ chế này: lần gửi lại của
    // cùng một thao tác phải mang cùng khóa, nếu không server thấy hai thao
    // tác khác nhau và ghi hai lần (RT-12).
    expect(headersOf(fetchMock)['X-Idempotency-Key']).toBeUndefined()

    fetchMock.mockClear()
    const key = newIdempotencyKey()
    await client.post('/api/v1/system/branches', { code: 'CN1' }, { idempotencyKey: key })
    expect(headersOf(fetchMock)['X-Idempotency-Key']).toBe(key)
  })

  it('dựng ApiError mang mã lỗi của server', async () => {
    fetchMock.mockResolvedValue(problemResponse('auth.invalid_credentials', 401))
    const client = new ApiClient({ baseUrl: BASE })

    await expect(client.post('/api/v1/auth/login', {})).rejects.toMatchObject({
      errorCode: 'auth.invalid_credentials',
      status: 401,
    })
  })

  it('phiên hết hiệu lực thì báo lên đúng một lần và quên token', async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(problemResponse('auth.not_authenticated', 401)),
    )
    const onSessionLost = vi.fn()
    const client = new ApiClient({ baseUrl: BASE, onSessionLost })
    client.setToken('token-cu')

    await expect(client.get('/api/v1/auth/me')).rejects.toBeInstanceOf(ApiError)
    expect(onSessionLost).toHaveBeenCalledTimes(1)

    // Quên token ngay tại đây: một truy vấn còn treo sau khi màn hình đã quay
    // về đăng nhập vẫn gửi token cũ, và token đó thường **vẫn còn hiệu lực**
    // phía server.
    fetchMock.mockClear()
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})))
    await client.get('/health')
    expect(headersOf(fetchMock).Authorization).toBeUndefined()
  })

  it.each([
    ['auth.invalid_credentials', 'nhập lại mật khẩu hiện tại sai'],
    ['auth.totp_code_invalid', 'gõ sai mã 2FA khi đăng ký thiết bị'],
    ['auth.totp_required', 'server đòi thêm mã 2FA'],
  ])('401 %s KHÔNG hủy phiên (%s)', async (errorCode) => {
    // Server dùng `401` cho hai chuyện khác hẳn nhau. Gộp lại thì gõ nhầm một
    // chữ ở ô mật khẩu sẽ đá người dùng về màn hình đăng nhập, không một dòng
    // giải thích — đo được qua giao diện ở review lát 2C-1.
    fetchMock.mockImplementation(() => Promise.resolve(problemResponse(errorCode, 401)))
    const onSessionLost = vi.fn()
    const client = new ApiClient({ baseUrl: BASE, onSessionLost })
    client.setToken('phien-con-song')

    await expect(client.post('/api/v1/auth/change-password', {})).rejects.toMatchObject({
      errorCode,
    })

    expect(onSessionLost).not.toHaveBeenCalled()
    fetchMock.mockClear()
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse({})))
    await client.get('/api/v1/auth/me')
    expect(headersOf(fetchMock).Authorization).toBe('Bearer phien-con-song')
  })

  it('426 lúc chạy báo lên để lớp phiên bật chế độ chỉ đọc', async () => {
    // `min_client_version` đổi được **trong lúc** máy trạm đang mở: người quản
    // trị nâng cấp máy chủ giữa ngày làm việc thì lượt bắt tay lúc khởi động
    // không còn là câu trả lời cuối cùng.
    fetchMock.mockImplementation(() =>
      Promise.resolve(problemResponse('system.client_version_unsupported', 426)),
    )
    const onClientTooOld = vi.fn()
    const client = new ApiClient({ baseUrl: BASE, onClientTooOld })

    await expect(client.post('/api/v1/system/branches', {})).rejects.toMatchObject({
      isClientTooOld: true,
    })

    expect(onClientTooOld).toHaveBeenCalledTimes(1)
  })

  it('máy chủ không tới được cũng cho ra ApiError, không phải TypeError', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const client = new ApiClient({ baseUrl: BASE })

    // Chỗ gọi chỉ phải bắt **một** loại lỗi. Để `TypeError` của fetch lọt ra
    // ngoài nghĩa là mỗi màn hình phải nhớ bắt thêm một thứ nữa.
    await expect(client.handshake()).rejects.toMatchObject({
      errorCode: 'transport.unreachable',
    })
  })

  it('phản hồi 204 không cố đọc thân rỗng thành JSON', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }))
    const client = new ApiClient({ baseUrl: BASE })

    await expect(client.post('/api/v1/auth/logout')).resolves.toBeUndefined()
  })

  it('postBlob phân nhánh theo Content-Type: tệp thì trả blob kèm header', async () => {
    fetchMock.mockResolvedValue(
      new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46]), {
        status: 200,
        headers: {
          'Content-Type': 'application/pdf',
          'Content-Disposition': 'attachment; filename="GLE26-00001-lan-2.pdf"',
          'X-Print-Copy-No': '2',
          'X-Print-Reprint': 'true',
        },
      }),
    )
    const client = new ApiClient({ baseUrl: BASE })

    const outcome = await client.postBlob('/api/v1/vouchers/x/print', {})
    expect(outcome.kind).toBe('file')
    if (outcome.kind === 'file') {
      expect(outcome.fileName).toBe('GLE26-00001-lan-2.pdf')
      expect(outcome.headers.get('X-Print-Reprint')).toBe('true')
    }
  })

  it('postBlob phân nhánh theo Content-Type: JSON (202 chuyển-job) thì trả thân đã parse', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ job_id: 'j-1', estimated_rows: 50000 }, 202),
    )
    const client = new ApiClient({ baseUrl: BASE })

    const outcome = await client.postBlob('/api/v1/reports/S03b-DN/render', {
      format: 'pdf',
      params: {},
    })
    expect(outcome.kind).toBe('json')
    if (outcome.kind === 'json') {
      expect(outcome.status).toBe(202)
      expect(outcome.data).toEqual({ job_id: 'j-1', estimated_rows: 50000 })
    }
  })

  it('postBlob vẫn ném ApiError mang mã lỗi của server', async () => {
    fetchMock.mockResolvedValue(problemResponse('report.render_not_ready', 409))
    const client = new ApiClient({ baseUrl: BASE })

    await expect(client.postBlob('/api/v1/reports/x/render', {})).rejects.toMatchObject({
      errorCode: 'report.render_not_ready',
    })
  })
})
