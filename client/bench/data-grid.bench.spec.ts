/**
 * Spike S3 — lưới nhập liệu có theo kịp người gõ không.
 *
 * Ngưỡng lấy nguyên từ plan (phase 2, bước 17 / Success Criteria):
 *
 * | Phép đo | Ngưỡng |
 * | --- | --- |
 * | Độ trễ phím trên lưới 500 dòng | < 50ms |
 * | Dán 200 dòng từ bảng tính | < 1s |
 * | Gõ tiếng Việt liên tục | không mất ký tự |
 *
 * Ba điều về cách đo, vì một bài đo sai còn tệ hơn không đo:
 *
 * 1. **Đo tới lúc đã vẽ, không tới lúc hàm chạy xong.** Trang đo bắt `keydown`
 *    ở pha capture rồi chờ hai lượt `requestAnimationFrame` — lượt sau chạy sau
 *    khi trình duyệt đã vẽ. Đo "thời gian chạy JavaScript" thì lưới nào cũng
 *    đạt, kể cả lưới giật.
 * 2. **Nhìn p95 và max, không nhìn trung bình.** Người gõ không cảm nhận số
 *    trung bình; họ cảm nhận đúng những lần khựng. Trung bình 8ms với vài lần
 *    120ms vẫn là một lưới khó chịu.
 * 3. **Tổ hợp IME dựng bằng CDP `Input.imeSetComposition`** — đây là đường
 *    composition thật của Chromium, không phải sự kiện tự chế. Bài test đơn vị
 *    ở `data-grid.test.tsx` bắn `compositionstart/end` bằng tay và chỉ chứng
 *    minh component phản ứng đúng với sự kiện; chỗ này chứng minh trình duyệt
 *    thật sinh ra đúng những sự kiện ấy.
 *
 * Vẫn còn một khoảng bắt buộc kiểm bằng tay: bộ gõ tiếng Việt **thật**
 * (EVKey/OpenKey/bộ gõ macOS) chạy ngoài trình duyệt và không máy nào giả được.
 * Danh mục kiểm tay nằm trong báo cáo lát 2C-3.
 */

import { expect, test } from '@playwright/test'

const KEY_LATENCY_BUDGET_MS = 50
/**
 * Ngưỡng cho **một** mẫu tệ nhất, nới gấp đôi so với ngưỡng p95.
 *
 * Không phải nới cho dễ đạt: `max` trên 48 mẫu đo trên một runner CI dùng
 * chung dao động 22–46ms giữa các lượt chạy dù mã không đổi, nên đóng cổng ở
 * đúng 50ms là dựng một bài test thỉnh thoảng đỏ vì lý do không liên quan — và
 * một cổng như thế sẽ bị người ta bỏ qua trước khi nó kịp bắt lỗi thật. Hồi
 * quy thật thì lớn hơn thế nhiều: gỡ cuộn ảo ra, `max` lên 112ms và p95 lên
 * 69,7ms (đo được, không phải suy luận).
 */
const KEY_LATENCY_OUTLIER_BUDGET_MS = KEY_LATENCY_BUDGET_MS * 2
const PASTE_BUDGET_MS = 1_000
const PASTE_ROWS = 200

interface GridBenchResult {
  readonly keySamples: readonly number[]
  readonly lastPasteMs: number | null
  readonly commits: number
}

function percentile(samples: readonly number[], fraction: number): number {
  const sorted = [...samples].sort((a, b) => a - b)
  const index = Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))
  return sorted[index] ?? 0
}

function describeSamples(label: string, samples: readonly number[]): string {
  return [
    `${label}: n=${samples.length}`,
    `p50=${percentile(samples, 0.5).toFixed(1)}ms`,
    `p95=${percentile(samples, 0.95).toFixed(1)}ms`,
    `max=${Math.max(...samples).toFixed(1)}ms`,
  ].join(' · ')
}

async function readBench(page: import('@playwright/test').Page): Promise<GridBenchResult> {
  return page.evaluate(() => ({
    keySamples: globalThis.__gridBench?.keySamples ?? [],
    lastPasteMs: globalThis.__gridBench?.lastPasteMs ?? null,
    commits: globalThis.__gridBench?.commits ?? 0,
  }))
}

/** Gõ vào ô đang chọn rồi trả về số đo độ trễ từng phím. */
async function typeAndMeasure(
  page: import('@playwright/test').Page,
  text: string,
): Promise<readonly number[]> {
  await page.getByRole('textbox').click()
  await page.evaluate(() => globalThis.__gridBench?.reset())
  // 12ms/phím ≈ 100 từ/phút — nhanh hơn hầu hết người nhập liệu, cố ý.
  await page.keyboard.type(text, { delay: 12 })
  // Chờ lượt `requestAnimationFrame` cuối cùng ghi xong mẫu đo.
  await page.waitForTimeout(200)
  const { keySamples } = await readBench(page)
  return keySamples
}

test.describe('Lưới nhập liệu 500 dòng', () => {
  test('chỉ dựng MỘT ô nhập và một cửa sổ dòng, không phải cả 500 dòng', async ({ page }) => {
    await page.goto('/bench/data-grid?rows=500')
    await expect(page.getByRole('grid')).toHaveAttribute('aria-rowcount', '501')

    expect(await page.getByRole('textbox').count()).toBe(1)
    // Đây là cơ chế làm nên hai phép đo dưới đây; mất nó thì số đo mất theo.
    expect(await page.getByRole('row').count()).toBeLessThan(40)
  })

  test('độ trễ phím dưới ngưỡng — chế độ cam kết khi rời ô', async ({ page }, testInfo) => {
    await page.goto('/bench/data-grid?rows=500')
    const samples = await typeAndMeasure(page, 'Vat tu nhap lieu 1234567890 abcdefghijklmnopqrst')

    const summary = describeSamples('on-leave', samples)
    testInfo.annotations.push({ type: 'đo được', description: summary })
    console.log(summary)

    expect(samples.length).toBeGreaterThan(30)
    expect(percentile(samples, 0.95)).toBeLessThan(KEY_LATENCY_BUDGET_MS)
    expect(Math.max(...samples)).toBeLessThan(KEY_LATENCY_OUTLIER_BUDGET_MS)
  })

  test('độ trễ phím dưới ngưỡng — kể cả khi cam kết từng phím', async ({ page }, testInfo) => {
    // Chế độ đắt nhất: mỗi ký tự kéo theo một lượt vẽ lại cả cửa sổ dòng. Đây
    // là chỗ số đo trả lời được câu hỏi thật của spike — cuộn ảo có đủ hay
    // không — chứ chế độ mặc định thì gõ gần như không chạm tới React.
    await page.goto('/bench/data-grid?rows=500&mode=live')
    const samples = await typeAndMeasure(page, 'Vat tu nhap lieu 1234567890 abcdefghijklmnopqrst')

    const summary = describeSamples('live', samples)
    testInfo.annotations.push({ type: 'đo được', description: summary })
    console.log(summary)

    // Đây mới là phép đo canh cuộn ảo. Chế độ `on-leave` không vẽ lại gì trong
    // lúc gõ nên số của nó gần như không đổi dù có cuộn ảo hay không — đo được:
    // gỡ cuộn ảo ra, `on-leave` giữ nguyên p95 17,9ms còn `live` nhảy lên 69,7ms.
    expect(percentile(samples, 0.95)).toBeLessThan(KEY_LATENCY_BUDGET_MS)
    expect(Math.max(...samples)).toBeLessThan(KEY_LATENCY_OUTLIER_BUDGET_MS)
  })

  test(`dán ${PASTE_ROWS} dòng từ bảng tính dưới 1 giây`, async ({ page, context }, testInfo) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.goto('/bench/data-grid?rows=500')

    // Vùng dán như copy từ Excel: 200 dòng × 5 cột, có cả ô bọc ngoặc kép.
    //
    // Tiền tố `DAN-` là bắt buộc, không phải cho đẹp: bản đầu của bài đo dùng
    // `VT00199` — **trùng đúng chuỗi** với dữ liệu mầm `makeLine(199).code` —
    // nên phép kiểm chống-rỗng thấy chuỗi ấy dù có dán hay không.
    const clipboard = Array.from({ length: PASTE_ROWS }, (_, row) =>
      [
        `DAN-${String(row).padStart(5, '0')}`,
        row % 20 === 0 ? '"Vật tư có\ndiễn giải xuống dòng"' : `Vật tư dán số ${row}`,
        'Cái',
        `${row + 1}`,
        `${row * 1000 + 50000}`,
      ].join('\t'),
    ).join('\n')

    await page.evaluate(async (text) => navigator.clipboard.writeText(text), clipboard)
    await page.getByRole('textbox').click()
    await page.evaluate(() => globalThis.__gridBench?.reset())
    await page.keyboard.press('ControlOrMeta+V')

    await expect
      .poll(async () => (await readBench(page)).lastPasteMs, { timeout: 5_000 })
      .not.toBeNull()
    const { lastPasteMs } = await readBench(page)

    const { commits } = await readBench(page)
    const summary = `dán ${PASTE_ROWS} dòng × 5 cột: ${(lastPasteMs ?? 0).toFixed(0)}ms · ${commits} ô`
    testInfo.annotations.push({ type: 'đo được', description: summary })
    console.log(summary)

    expect(lastPasteMs).toBeLessThan(PASTE_BUDGET_MS)

    // Số đo chỉ có nghĩa nếu vùng dán thật sự vào **đủ** ô. Không có hai phép
    // kiểm dưới đây thì một trình xử lý dán hỏng hoàn toàn — chỉ áp dụng dòng
    // đầu, lệch một dòng, hay không làm gì cả — vẫn cho ra một con số "nhanh".
    expect(commits).toBe(PASTE_ROWS * 5)

    // Vùng dán phải bắt đầu ĐÚNG ở ô neo. Không có phép kiểm này thì một vùng
    // dán lệch xuống một dòng vẫn cho đủ 1.000 ô và vẫn thấy dòng cuối —
    // "đủ số lượng, sai vị trí" là loại sai không ai nhìn ra trên 200 dòng.
    await expect(page.getByRole('textbox')).toHaveValue('DAN-00000')

    await page.mouse.wheel(0, 30 * 195)
    await expect(page.getByRole('row', { name: /DAN-00199/ })).toBeVisible()
  })

  test('tổ hợp IME thật của Chromium không bị lưới cắt ngang', async ({ page, context }) => {
    await page.goto('/bench/data-grid?rows=500')
    // `fill('')` chứ không `Control+A` + `Delete`: trên macOS `Control+A` là
    // "về đầu dòng", nên phép dọn ô im lặng không dọn gì và bài test đo nhầm.
    await page.getByRole('textbox').fill('')

    const cdp = await context.newCDPSession(page)
    // Telex: "dduwowcj" → "được". Bộ gõ dựng dần chuỗi đang soạn rồi chốt.
    for (const stage of ['d', 'dd', 'đ', 'đu', 'đuơ', 'được']) {
      await cdp.send('Input.imeSetComposition', {
        text: stage,
        selectionStart: stage.length,
        selectionEnd: stage.length,
      })
    }
    // **Phím Enter thật**, không phải `Input.insertText`.
    //
    // Bản đầu của bài đo chốt tổ hợp bằng `insertText`, tức là bỏ qua đúng phím
    // mà comment ngay trên nó nói tới — và bằng chứng là gỡ HẲN hàng rào IME
    // trong `handleKeyDown` thì bài đo vẫn xanh. Enter kết thúc tổ hợp Telex và
    // Enter xuống dòng dưới là cùng một sự kiện `keydown`; chỉ phím thật mới
    // phân biệt được hai vế đó.
    await page.keyboard.press('Enter')

    await expect(page.getByRole('textbox')).toHaveValue('được')
    // Vẫn đứng ở dòng 1: tổ hợp IME không được làm lưới nhảy ô.
    await expect(page.getByRole('textbox')).toHaveAttribute('aria-label', /dòng 1$/)
  })

  test('cuộn dòng đang gõ ra khỏi cửa sổ vẫn giữ được chữ vừa gõ', async ({ page }) => {
    await page.goto('/bench/data-grid?rows=500')
    await page.getByRole('textbox').fill('')
    await page.keyboard.type('KHONGMAT')

    // Lăn chuột xuống xem tổng cộng ở cuối lưới là thao tác hằng ngày. Ô nhập
    // bị tháo, và trình duyệt KHÔNG bắn `blur` khi phần tử đang focus bị gỡ
    // khỏi DOM — đo được trước khi sửa: 0 lượt cam kết, cuộn lên thì dòng trống.
    // `hover()` trước khi lăn: con lăn chuột tác động vào phần tử dưới con trỏ,
    // và `fill()` không di chuột nên nếu bỏ dòng này thì trang cuộn chứ không
    // phải lưới — bài test sẽ xanh mà chẳng kiểm gì.
    await page.getByRole('grid').hover()
    await page.mouse.wheel(0, 30 * 120)
    await expect(page.getByRole('textbox')).toHaveCount(0)
    await expect
      .poll(async () => (await readBench(page)).commits)
      .toBeGreaterThan(0)

    await page.mouse.wheel(0, -30 * 130)
    await expect(page.getByRole('row', { name: /KHONGMAT/ })).toBeVisible()
  })
})
