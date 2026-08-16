/**
 * So sánh phiên bản — đối trọng phía client của
 * `server/tests/test_client_version_gate.py`.
 *
 * Hai bộ test khóa **cùng một** bộ giá trị biên vì hai bên phải kết luận giống
 * nhau: server từ chối `426`, còn client phải biết trước điều đó để hiện màn
 * hình cập nhật thay vì để người dùng gõ xong rồi mới hỏng.
 */

import { describe, expect, it } from 'vitest'

import { checkVersion, compareVersions, parseVersion } from '@/lib/app-version'

describe('parseVersion', () => {
  it('đọc được ba số nguyên', () => {
    expect(parseVersion('1.2.3')).toEqual([1, 2, 3])
    expect(parseVersion(' 0.6.0 ')).toEqual([0, 6, 0])
  })

  it.each(['v1.2.3', '1.2', '1.2.3-beta', '1.2.3.4', '', 'latest', '99999.0.0'])(
    'từ chối chuỗi không đúng khuôn: %s',
    (raw) => {
      expect(parseVersion(raw)).toBeNull()
    },
  )
})

describe('compareVersions', () => {
  it('so theo số chứ không theo thứ tự từ điển', () => {
    // Chỗ này là lý do tồn tại của cả tệp: `'0.10.0' < '0.9.0'` nếu so chuỗi,
    // và triệu chứng sẽ là cả văn phòng bị đá sang màn hình "cần cập nhật"
    // ngay sau một bản phát hành bình thường.
    expect(compareVersions([0, 10, 0], [0, 9, 0])).toBe(1)
    expect(compareVersions([1, 0, 0], [0, 99, 99])).toBe(1)
    expect(compareVersions([1, 2, 3], [1, 2, 3])).toBe(0)
  })
})

describe('checkVersion', () => {
  const handshake = { min_client_version: '1.4.0', server_version: '1.5.0' }

  it('bản cũ hơn mức tối thiểu phải bị chặn', () => {
    expect(checkVersion('1.3.9', handshake)).toBe('client-too-old')
  })

  it('đúng mức tối thiểu là đi tiếp được', () => {
    expect(checkVersion('1.4.0', handshake)).toBe('ok')
  })

  it('mới hơn máy chủ chỉ cảnh báo, không chặn', () => {
    // Bất đối xứng có chủ đích: việc phải làm nằm ở máy chủ, còn chặn client
    // ở đây chỉ làm người dùng mất việc đang gõ dở mà không sửa được gì.
    expect(checkVersion('1.6.0', handshake)).toBe('server-behind')
  })

  it('chuỗi không đọc được thì coi như không kiểm được', () => {
    expect(checkVersion('bản mới nhất', handshake)).toBe('unreadable')
    expect(checkVersion('1.4.0', { min_client_version: 'x', server_version: '1.5.0' })).toBe(
      'unreadable',
    )
  })
})
