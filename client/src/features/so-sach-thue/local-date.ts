/**
 * Ngày hôm nay theo múi giờ máy khách, dạng `YYYY-MM-DD`.
 *
 * `Date.prototype.toISOString()` quy về UTC — ở múi giờ dương (Việt Nam,
 * UTC+7), sau 17:00 giờ máy khách nó đã sang ngày hôm sau theo UTC. Ngày hạch
 * toán mặc định của chứng từ mới phải là ngày người dùng đang thấy trên máy.
 */
export function todayIso(): string {
  const now = new Date()
  const year = String(now.getFullYear())
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}
