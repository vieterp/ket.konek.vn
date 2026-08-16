/**
 * Phiên bản bản client và phép so sánh với những gì máy chủ đòi hỏi.
 *
 * Đối trọng phía client của `server/src/ket/kernel/versions.py`: **cùng một**
 * luật `MAJOR.MINOR.PATCH`, cố ý viết lại chứ không sinh tự động. Hai bản có
 * thể lệch nhau nếu ai đó đổi luật ở một phía — nhưng luật này là ba số nguyên,
 * và cái giá của việc dựng một đường sinh mã cho mười dòng lớn hơn cái giá của
 * việc chép lại chúng. Test hai phía cùng khóa một bộ giá trị biên.
 *
 * Vì sao không so chuỗi: `'0.10.0' < '0.9.0'` theo thứ tự từ điển, tức là bản
 * mới hơn bị coi là cũ hơn — và triệu chứng của lỗi đó là cả văn phòng bị đá
 * sang màn hình "cần cập nhật" ngay sau một bản phát hành bình thường.
 */

/** Phiên bản đang chạy, do bộ dựng thay vào từ `client/package.json`. */
export const APP_VERSION: string = __APP_VERSION__

export type Version = readonly [number, number, number]

const VERSION_PATTERN = /^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$/

/** `"1.2.3"` → `[1, 2, 3]`. Chuỗi không đúng khuôn → `null`. */
export function parseVersion(raw: string): Version | null {
  const match = VERSION_PATTERN.exec(raw.trim())
  if (match === null) {
    return null
  }
  const [, major, minor, patch] = match
  return [Number(major), Number(minor), Number(patch)]
}

/** `-1` nếu `a` cũ hơn `b`, `0` nếu bằng, `1` nếu mới hơn. */
export function compareVersions(a: Version, b: Version): number {
  for (let index = 0; index < 3; index += 1) {
    const left = a[index] ?? 0
    const right = b[index] ?? 0
    if (left !== right) {
      return left < right ? -1 : 1
    }
  }
  return 0
}

/** Kết luận của lần bắt tay — quyết định màn hình đầu tiên người dùng thấy. */
export type VersionVerdict =
  | 'ok'
  /** Client cũ hơn `min_client_version`: máy chủ sẽ từ chối mọi lệnh ghi. */
  | 'client-too-old'
  /** Client mới hơn máy chủ: việc phải làm nằm ở máy chủ, client chỉ cảnh báo. */
  | 'server-behind'
  /** Một trong các chuỗi phiên bản không đọc được — coi như không kiểm được. */
  | 'unreadable'

/**
 * So bản đang chạy với những gì máy chủ vừa khai ở `/system/handshake`.
 *
 * `client-too-old` **chặn**; `server-behind` chỉ cảnh báo. Lý do bất đối xứng:
 * máy chủ cũ hơn client vẫn nhận được mọi lệnh ghi mà client gửi (hợp đồng
 * OpenAPI của nó là tập con), còn client cũ hơn `min_client_version` thì mọi
 * lệnh ghi sẽ trả `426` — dừng người dùng ở màn hình cập nhật rẻ hơn nhiều so
 * với để họ gõ xong một chứng từ rồi mới biết.
 */
export function checkVersion(
  clientVersion: string,
  handshake: { readonly min_client_version: string; readonly server_version: string },
): VersionVerdict {
  const client = parseVersion(clientVersion)
  const minimum = parseVersion(handshake.min_client_version)
  const server = parseVersion(handshake.server_version)
  if (client === null || minimum === null || server === null) {
    return 'unreadable'
  }
  if (compareVersions(client, minimum) < 0) {
    return 'client-too-old'
  }
  if (compareVersions(client, server) > 0) {
    return 'server-behind'
  }
  return 'ok'
}
