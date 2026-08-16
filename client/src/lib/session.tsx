/**
 * Phiên đăng nhập và trạng thái khởi động của ứng dụng.
 *
 * Đây là máy trạng thái mà mọi màn hình khác dựa vào, và nó có **năm** trạng
 * thái vì server có năm câu trả lời khác nhau cho cùng một tài khoản (2B-1a,
 * 2B-1b, bước 19):
 *
 * ```
 *   starting ──handshake──► update-required   (client cũ hơn min_client_version)
 *      │                └─► unreachable       (không tới được máy chủ)
 *      ▼
 *   anonymous ──login──┬─► password-change    (must_change_password)
 *                      ├─► totp-enrollment    (session_scope = totp_enrollment)
 *                      └─► ready
 * ```
 *
 * Trạng thái **không** suy ra từ mã lỗi rải rác trong từng màn hình mà lấy
 * thẳng từ `/auth/me`: cùng một tài khoản, sau khi tải lại trang, phải rơi vào
 * đúng màn hình đó. Suy ra từ phản hồi của lần đăng nhập thôi thì tải lại trang
 * sẽ đưa người dùng vào thẳng phần nghiệp vụ với một phiên hạn chế — server vẫn
 * chặn, nhưng chặn bằng một chuỗi `403` khó hiểu ở mọi màn hình.
 *
 * Sau khi đăng nhập, `token` **luôn** đi qua `/auth/me` một lượt nữa dù lệnh
 * đăng nhập đã trả đủ thông tin. Lý do: khôi phục phiên (mở lại trang) và đăng
 * nhập mới phải đi **cùng một** đường mã, nếu không thì nhánh ít chạy hơn —
 * khôi phục phiên — là nhánh sẽ hỏng mà không ai thấy.
 */

import type { ReactElement, ReactNode } from 'react'
import { createContext, use, useCallback, useEffect, useMemo, useState } from 'react'

import type { Schemas } from '@api-types'

import type { Handshake } from '@/lib/api-client'
import { ApiClient, ApiError, resolveServerUrl } from '@/lib/api-client'
import { APP_VERSION, checkVersion } from '@/lib/app-version'
import {
  clearStoredSession,
  readStoredSession,
  writeStoredSession,
} from '@/lib/session-storage'

type Me = Schemas['MeResponse']
type LoginResponse = Schemas['LoginResponse']

export type SessionStage =
  | 'starting'
  | 'unreachable'
  | 'update-required'
  | 'anonymous'
  | 'password-change'
  | 'totp-enrollment'
  | 'ready'

export interface SessionState {
  readonly stage: SessionStage
  readonly serverUrl: string
  readonly handshake: Handshake | null
  readonly me: Me | null
  /** Dữ liệu kế toán đang mở; `null` = chưa chọn (màn hình chọn bộ sổ). */
  readonly datasetCode: string | null
  /**
   * Bộ sổ mà người dùng vừa rời khỏi vì không có quyền.
   *
   * Màn hình chọn bộ sổ dùng nó để **không** tự chọn lại chính bộ sổ đó khi bản
   * cài chỉ có một dữ liệu kế toán.
   */
  readonly rejectedDatasetCode: string | null
  /**
   * Máy chủ sẽ từ chối mọi lệnh ghi của bản client này (`426`).
   *
   * Giữ **cả sau khi** người dùng bấm "tiếp tục ở chế độ chỉ đọc": màn hình
   * dùng nó để ẩn nút lưu, thay vì để người dùng gõ xong rồi mới nhận lỗi.
   */
  readonly readOnly: boolean
}

interface SessionApi extends SessionState {
  readonly client: ApiClient
  readonly login: (username: string, password: string, totpCode?: string) => Promise<void>
  readonly logout: () => Promise<void>
  /** Nạp lại `/auth/me` — gọi sau khi đổi mật khẩu hoặc xác nhận thiết bị 2FA. */
  readonly refresh: () => Promise<void>
  readonly selectDataset: (code: string) => void
  /** Bỏ chọn dữ liệu kế toán để quay lại màn hình chọn bộ sổ. */
  readonly clearDataset: () => void
  /** Rời màn hình "cần cập nhật" để tra cứu sổ sách (lệnh ghi vẫn bị từ chối). */
  readonly continueReadOnly: () => void
}

const SessionContext = createContext<SessionApi | null>(null)

function stageFor(me: Me): SessionStage {
  if (me.session_scope === 'totp_enrollment') {
    return 'totp-enrollment'
  }
  if (me.must_change_password) {
    return 'password-change'
  }
  return 'ready'
}

export function SessionProvider({ children }: { children: ReactNode }): ReactElement {
  const [serverUrl] = useState(resolveServerUrl)

  const [stage, setStage] = useState<SessionStage>('starting')
  const [handshake, setHandshake] = useState<Handshake | null>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [datasetCode, setDatasetCode] = useState<string | null>(null)
  const [readOnly, setReadOnly] = useState(false)
  const [rejectedDatasetCode, setRejectedDatasetCode] = useState<string | null>(null)

  // Client dựng **một lần** cho cả vòng đời ứng dụng: nó giữ token bên trong
  // (`setToken`), nên không có gì ở đây phải vẽ lại khi người dùng đăng nhập.
  const [client] = useState(
    () =>
      new ApiClient({
        baseUrl: serverUrl,
        // Phiên bị thu hồi từ phía server (đổi mật khẩu ở máy khác, người quản
        // trị đuổi phiên) phải đưa người dùng về màn hình đăng nhập ngay, chứ
        // không để họ bấm tiếp vào một loạt màn hình rỗng.
        //
        // **Chỉ** cho `auth.not_authenticated`. `401` của "gõ sai mật khẩu hiện
        // tại" hay "sai mã 2FA" để màn hình tự xử lý — xem `ApiError.isSessionLost`.
        onSessionLost: () => {
          clearStoredSession(serverUrl)
          setMe(null)
          setDatasetCode(null)
          setStage('anonymous')
        },
        // `min_client_version` đổi được trong lúc máy trạm đang mở, nên lượt bắt
        // tay lúc khởi động không phải câu trả lời cuối cùng.
        onClientTooOld: () => {
          setReadOnly(true)
        },
      }),
  )

  const forgetSession = useCallback(() => {
    client.setToken(null)
    clearStoredSession(serverUrl)
    setMe(null)
    setDatasetCode(null)
  }, [client, serverUrl])

  /** Đọc `/auth/me` bằng token đang giữ và đặt trạng thái theo câu trả lời. */
  const adoptToken = useCallback(
    async (token: string, expiresAt: string, storedDataset: string | null): Promise<void> => {
      client.setToken(token)
      const identity = await client.get<Me>('/api/v1/auth/me')
      writeStoredSession(serverUrl, { token, expiresAt, datasetCode: storedDataset })
      setMe(identity)
      setDatasetCode(storedDataset)
      setStage(stageFor(identity))
    },
    [client, serverUrl],
  )

  // Khởi động: bắt tay trước, khôi phục phiên sau. Thứ tự bắt buộc — một bản
  // client quá cũ phải thấy màn hình cập nhật ngay, chứ không phải sau khi nó
  // đã vào tới màn hình nghiệp vụ rồi mới nhận `426` ở lần lưu đầu tiên.
  useEffect(() => {
    let cancelled = false

    async function boot(): Promise<void> {
      let greeting: Handshake
      try {
        greeting = await client.handshake()
      } catch {
        if (!cancelled) {
          setStage('unreachable')
        }
        return
      }
      if (cancelled) {
        return
      }
      setHandshake(greeting)

      const verdict = checkVersion(APP_VERSION, greeting)
      if (verdict === 'client-too-old' || verdict === 'unreadable') {
        setReadOnly(true)
        setStage('update-required')
        return
      }

      const stored = readStoredSession(serverUrl)
      if (stored === null) {
        setStage('anonymous')
        return
      }
      try {
        await adoptToken(stored.token, stored.expiresAt, stored.datasetCode)
      } catch {
        // Token hết hạn hoặc đã bị thu hồi. `onSessionLost` đã dọn và đặt
        // `anonymous`; ở đây chỉ cần không để ứng dụng kẹt ở `starting` khi lỗi
        // là loại khác (máy chủ vừa tắt giữa chừng).
        if (!cancelled) {
          setStage('anonymous')
        }
      }
    }

    void boot()
    return () => {
      cancelled = true
    }
  }, [adoptToken, client, serverUrl])

  const login = useCallback(
    async (username: string, password: string, totpCode?: string): Promise<void> => {
      const issued = await client.post<LoginResponse>('/api/v1/auth/login', {
        username,
        password,
        ...(totpCode === undefined ? {} : { totp_code: totpCode }),
      })
      await adoptToken(issued.token, issued.expires_at, null)
    },
    [adoptToken, client],
  )

  const logout = useCallback(async (): Promise<void> => {
    try {
      await client.post<void>('/api/v1/auth/logout')
    } catch {
      // Token đã hết hạn hoặc máy chủ không tới được: phiên phía client vẫn
      // phải biến mất. Giữ lại token sau khi người dùng bấm Đăng xuất là hành
      // vi tệ nhất có thể ở một máy trạm dùng chung.
    }
    forgetSession()
    setStage('anonymous')
  }, [client, forgetSession])

  const refresh = useCallback(async (): Promise<void> => {
    const identity = await client.get<Me>('/api/v1/auth/me')
    setMe(identity)
    setStage(stageFor(identity))
  }, [client])

  const selectDataset = useCallback(
    (code: string): void => {
      setDatasetCode(code)
      setRejectedDatasetCode(null)
      const stored = readStoredSession(serverUrl)
      if (stored !== null) {
        writeStoredSession(serverUrl, { ...stored, datasetCode: code })
      }
    },
    [serverUrl],
  )

  const clearDataset = useCallback((): void => {
    // Nhớ mã vừa rời khỏi: màn hình chọn bộ sổ tự chọn khi bản cài chỉ có một
    // dữ liệu kế toán, nên không nhớ thì người bị từ chối quyền sẽ bị đưa
    // ngược lại đúng bộ sổ đó — một vòng lặp không lối ra (đo được ở review).
    setDatasetCode((current) => {
      setRejectedDatasetCode(current)
      return null
    })
    const stored = readStoredSession(serverUrl)
    if (stored !== null) {
      writeStoredSession(serverUrl, { ...stored, datasetCode: null })
    }
  }, [serverUrl])

  const continueReadOnly = useCallback((): void => {
    setStage('anonymous')
  }, [])

  const value = useMemo<SessionApi>(
    () => ({
      stage,
      serverUrl,
      handshake,
      me,
      datasetCode,
      rejectedDatasetCode,
      readOnly,
      client,
      login,
      logout,
      refresh,
      selectDataset,
      clearDataset,
      continueReadOnly,
    }),
    [
      clearDataset,
      client,
      continueReadOnly,
      datasetCode,
      handshake,
      login,
      logout,
      me,
      readOnly,
      refresh,
      rejectedDatasetCode,
      selectDataset,
      serverUrl,
      stage,
    ],
  )

  return <SessionContext value={value}>{children}</SessionContext>
}

export function useSession(): SessionApi {
  const value = use(SessionContext)
  if (value === null) {
    throw new Error('useSession phải nằm trong <SessionProvider>')
  }
  return value
}

export { ApiError }
