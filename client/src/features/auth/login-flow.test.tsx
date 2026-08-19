/**
 * Máy trạng thái đăng nhập, kiểm qua giao diện thật.
 *
 * Đây là lý do lát này dựng bộ test client (quyết định H3). Server có **bốn**
 * câu trả lời khác nhau cho cùng một lần đăng nhập, và không câu nào kiểm được
 * bằng `tsc`:
 *
 * 1. `auth.totp_required` — hỏi thêm mã, **không** phải lỗi;
 * 2. `session_scope = totp_enrollment` — bắt đăng ký thiết bị trước;
 * 3. `must_change_password` — bắt đổi mật khẩu tạm trước;
 * 4. bắt tay thấy client cũ hơn `min_client_version` — màn hình cập nhật, và
 *    người dùng chưa kịp gõ gì cả.
 *
 * Test dựng **cả cây** (i18n → phiên → truy vấn → định tuyến) và chỉ giả lập
 * `fetch`. Giả lập ở tầng thấp hơn — mock `useSession` chẳng hạn — sẽ kiểm
 * chính đoạn mã mà test viết ra, không phải đoạn mã chạy ở nơi cài đặt.
 */

import type { ReactElement } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'
import { APP_VERSION } from '@/lib/app-version'

interface RouteReply {
  readonly status: number
  readonly body: unknown
}

/** Bàn định tuyến giả: khớp theo phần đuôi của URL. */
type Routes = Record<string, RouteReply | (() => RouteReply)>

const DEFAULT_ME = {
  user_id: 1,
  username: 'ke_toan',
  locale: 'vi',
  must_change_password: false,
  expires_at: '2026-08-17T00:00:00Z',
  session_scope: 'full',
}

function handshake(overrides: Record<string, string> = {}): RouteReply {
  return {
    status: 200,
    body: {
      server_version: APP_VERSION,
      min_client_version: APP_VERSION,
      control_schema_version: '4',
      deployment_mode: 'standalone',
      ...overrides,
    },
  }
}

function problem(errorCode: string, status: number): RouteReply {
  return {
    status,
    body: {
      type: `https://konek.vn/errors/${errorCode}`,
      title: errorCode,
      status,
      detail: 'câu dành cho người vận hành',
      error_code: errorCode,
    },
  }
}

function mockServer(routes: Routes): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((url: string) => {
    const match = Object.keys(routes).find((path) => url.endsWith(path))
    if (match === undefined) {
      return Promise.resolve(new Response('{}', { status: 404 }))
    }
    const entry = routes[match]
    const reply = typeof entry === 'function' ? entry() : (entry as RouteReply)
    return Promise.resolve(
      new Response(JSON.stringify(reply.body), {
        status: reply.status,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderApp(): ReactElement {
  return render(
    <AppProviders>
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<SessionGate />}>
            <Route index element={<p>Màn hình tổng quan</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </AppProviders>,
  ).container as unknown as ReactElement
}

async function signIn(): Promise<void> {
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('Tên đăng nhập'), 'ke_toan')
  await user.type(screen.getByLabelText('Mật khẩu'), 'mat-khau-dung')
  await user.click(screen.getByRole('button', { name: 'Đăng nhập' }))
}

const ONE_DATASET = {
  status: 200,
  body: { items: [{ code: 'alpha', name: 'Công ty Alpha', scheme: 'TT99' }] },
}

const ACCESS = {
  status: 200,
  body: { dataset_code: 'alpha', permissions: [], branch_ids: [1], acting_branch_id: 1 },
}

const ISSUED_SESSION = {
  status: 200,
  body: {
    token: 'phien-moi',
    expires_at: '2026-08-17T00:00:00Z',
    must_change_password: false,
    session_scope: 'full',
  },
}

describe('luồng đăng nhập', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('đăng nhập xuôi thì vào thẳng ứng dụng', async () => {
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': ISSUED_SESSION,
      '/auth/me': { status: 200, body: DEFAULT_ME },
      '/system/datasets': ONE_DATASET,
      '/system/access': ACCESS,
    })

    renderApp()
    await signIn()

    expect(await screen.findByText('Màn hình tổng quan')).toBeInTheDocument()
  })

  it('mã 2FA chỉ hiện khi server đòi, và mật khẩu không bị xóa', async () => {
    let asked = false
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': () => {
        if (!asked) {
          asked = true
          return problem('auth.totp_required', 401)
        }
        return ISSUED_SESSION
      },
      '/auth/me': { status: 200, body: DEFAULT_ME },
      '/system/datasets': ONE_DATASET,
      '/system/access': ACCESS,
    })

    renderApp()
    await signIn()

    // Không phải lỗi — chỉ là bước còn thiếu. Người dùng không phải gõ lại gì.
    const code = await screen.findByLabelText('Mã xác thực hai lớp')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Mật khẩu')).toHaveValue('mat-khau-dung')

    const user = userEvent.setup()
    await user.type(code, '123456')
    await user.click(screen.getByRole('button', { name: 'Đăng nhập' }))

    expect(await screen.findByText('Màn hình tổng quan')).toBeInTheDocument()
  })

  it('sai mật khẩu hiện thông điệp dựng từ mã lỗi, không phải câu của server', async () => {
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': problem('auth.invalid_credentials', 401),
    })

    renderApp()
    await signIn()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Tên đăng nhập hoặc mật khẩu không đúng.')
    // `detail` là câu cho người vận hành và cho log — không bao giờ hiện ra.
    expect(alert).not.toHaveTextContent('câu dành cho người vận hành')
  })

  it('mật khẩu tạm đưa thẳng tới màn hình đổi mật khẩu', async () => {
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': ISSUED_SESSION,
      '/auth/me': { status: 200, body: { ...DEFAULT_ME, must_change_password: true } },
    })

    renderApp()
    await signIn()

    expect(await screen.findByRole('heading', { name: 'Đổi mật khẩu' })).toBeInTheDocument()
    expect(screen.queryByText('Màn hình tổng quan')).not.toBeInTheDocument()
  })

  it('gõ sai mật khẩu hiện tại KHÔNG đá người dùng ra màn hình đăng nhập', async () => {
    // Server trả `401 auth.invalid_credentials` cho việc này, nhưng phiên vẫn
    // sống. Coi mọi `401` là "mất phiên" thì gõ nhầm một chữ là văng ra, không
    // một dòng giải thích — đo được qua giao diện ở review lát 2C-1.
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': ISSUED_SESSION,
      '/auth/me': { status: 200, body: { ...DEFAULT_ME, must_change_password: true } },
      '/auth/change-password': problem('auth.invalid_credentials', 401),
    })

    renderApp()
    await signIn()
    await screen.findByRole('heading', { name: 'Đổi mật khẩu' })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Mật khẩu hiện tại'), 'go-nham')
    await user.type(screen.getByLabelText('Mật khẩu mới'), 'MatKhauMoi!2026')
    await user.type(screen.getByLabelText('Nhập lại mật khẩu mới'), 'MatKhauMoi!2026')
    await user.click(screen.getByRole('button', { name: 'Đổi mật khẩu' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Tên đăng nhập hoặc mật khẩu không đúng.',
    )
    expect(screen.getByRole('heading', { name: 'Đổi mật khẩu' })).toBeInTheDocument()
  })

  it('chọn bộ sổ không có quyền thì nói rõ và cho chọn lại', async () => {
    // Danh sách dữ liệu kế toán **không** lọc theo vai trò (xem
    // `/system/datasets`), nên câu trả lời thật đến từ `/system/access`. Đo được
    // trên bản cài thật: không có nhánh này, người dùng rơi vào một vỏ ứng dụng
    // rỗng — không thông báo, không lối ra.
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': ISSUED_SESSION,
      '/auth/me': { status: 200, body: DEFAULT_ME },
      '/system/datasets': ONE_DATASET,
      '/system/access': problem('dataset.access_denied', 403),
    })

    renderApp()
    await signIn()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Tài khoản không có quyền trong dữ liệu kế toán này.',
    )

    // Lối ra phải **đi tới đâu đó**. Bản cài chỉ có một bộ sổ thì nút "đổi dữ
    // liệu kế toán" một mình tạo vòng lặp: chọn lại đúng bộ sổ đó rồi quay về
    // chính màn hình này. Hai nút, và cả hai phải thoát được.
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Đổi dữ liệu kế toán' }))

    expect(await screen.findByRole('heading', { name: 'Chọn dữ liệu kế toán' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Đăng xuất' })).toBeInTheDocument()
  })

  it('phiên hạn chế đưa thẳng tới màn hình đăng ký thiết bị 2FA', async () => {
    mockServer({
      '/system/handshake': handshake(),
      '/auth/login': ISSUED_SESSION,
      '/auth/me': { status: 200, body: { ...DEFAULT_ME, session_scope: 'totp_enrollment' } },
    })

    renderApp()
    await signIn()

    expect(
      await screen.findByRole('heading', { name: 'Đăng ký xác thực hai lớp' }),
    ).toBeInTheDocument()
  })
})

describe('bắt tay phiên bản', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('client cũ hơn mức tối thiểu thấy màn hình cập nhật trước cả ô mật khẩu', async () => {
    mockServer({
      '/system/handshake': handshake({ min_client_version: '99.0.0', server_version: '99.0.0' }),
    })

    renderApp()

    expect(await screen.findByRole('heading', { name: 'Cần cập nhật ứng dụng' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Mật khẩu')).not.toBeInTheDocument()
  })

  it('trong trình duyệt, nút cập nhật nói thẳng là không tự cập nhật được', async () => {
    mockServer({
      '/system/handshake': handshake({ min_client_version: '99.0.0', server_version: '99.0.0' }),
    })

    renderApp()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Cập nhật ngay' }))

    // Bộ test chạy trong jsdom, tức là không có shell Tauri — đúng bằng chế độ
    // "trình duyệt trong LAN" của v1.x. Ở đó phải nói thẳng thay vì hiện một
    // cái nút bấm vào không làm gì, vì người dùng đang bị chặn mọi lệnh ghi.
    expect(
      await screen.findByText(/Bản chạy trong trình duyệt không tự cập nhật được/),
    ).toBeInTheDocument()
  })

  it('vẫn tra cứu được sau khi chọn chế độ chỉ đọc', async () => {
    mockServer({
      '/system/handshake': handshake({ min_client_version: '99.0.0', server_version: '99.0.0' }),
      '/auth/login': ISSUED_SESSION,
      '/auth/me': { status: 200, body: DEFAULT_ME },
      '/system/datasets': ONE_DATASET,
      '/system/access': ACCESS,
    })

    renderApp()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Tiếp tục ở chế độ chỉ đọc' }))
    await signIn()

    expect(await screen.findByText('Màn hình tổng quan')).toBeInTheDocument()
    // Cờ chỉ-đọc đi theo suốt phiên: màn hình nghiệp vụ dùng nó để ẩn nút lưu
    // thay vì để người dùng gõ xong rồi nhận `426`.
    expect(screen.getByText('Chế độ chỉ đọc')).toBeInTheDocument()
  })

  it('không tới được máy chủ thì nói rõ địa chỉ đang gọi', async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')))
    vi.stubGlobal('fetch', fetchMock)

    renderApp()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(window.location.origin)
    })
  })
})
