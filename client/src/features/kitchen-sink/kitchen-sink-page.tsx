/**
 * Trang duyệt trực quan toàn bộ design system (bước 16 của phase 2).
 *
 * Đây là **công cụ nội bộ**, không phải màn hình sản phẩm:
 *
 * - Chỉ tồn tại ở bản dev — `router.tsx` gác bằng cờ build-time `__DEV_TOOLS__`
 *   (chỉ bật khi `vite serve`, xem `vite.config.ts`), nên bundle giao khách
 *   không có route này (quyết định H8); CI xác nhận bằng
 *   `.github/scripts/check-client-bundle.sh`.
 * - Nằm **ngoài** `SessionGate`: người thiết kế mở `pnpm dev` rồi vào thẳng
 *   `/kitchen-sink` để soi component, không cần server chạy, không cần tài
 *   khoản. Đó là toàn bộ lý do tồn tại của nó.
 * - Chữ trên trang hard-code tiếng Việt (quyết định H10): trang này không giao
 *   cho khách, nên nhét ~40 khóa vào cả `vi.ts` lẫn `en.ts` chỉ làm hai catalog
 *   thật khó đọc hơn. Chữ **của component** thì vẫn đi vào bằng prop như mọi
 *   chỗ gọi khác — đúng bằng cách màn hình nghiệp vụ sẽ gọi chúng.
 *
 * Vì sao không dùng Storybook: xem quyết định H8 ở phase-02. Tóm tắt — ở đây
 * component hiện trong đúng cái vỏ thật (token thật, chế độ tối thật, font
 * offline thật), và không phải nuôi thêm một bộ công cụ thứ hai.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import {
  Alert,
  Button,
  ChecklistPanel,
  DataGrid,
  DataTable,
  Drawer,
  NextActionCell,
  Seg,
  SelectField,
  SplitPane,
  StatusPill,
  Tabs,
  TextField,
} from '@/design-system/components'
import type {
  ChecklistItem,
  DataGridColumn,
  DataTableColumn,
  DataTableSort,
  DataTableTotalRow,
} from '@/design-system/components'
import { formatDate, formatMoney } from '@/lib/formatters'
import { THEME_PREFERENCES, useTheme } from '@/lib/theme'

interface DemoRow {
  readonly id: string
  readonly code: string
  readonly partner: string
  readonly issuedOn: string
  readonly amount: string
  readonly status: 'draft' | 'waiting' | 'accepted' | 'rejected'
  readonly nextAction: string | null
}

/** Dữ liệu mẫu — số tiền là **chuỗi** đúng như server trả về (không float). */
const DEMO_ROWS: readonly DemoRow[] = [
  {
    id: '1',
    code: 'HD-2026-000118',
    partner: 'Công ty TNHH Thiên Long',
    issuedOn: '2026-08-03',
    amount: '12500000.00',
    status: 'accepted',
    nextAction: null,
  },
  {
    id: '2',
    code: 'HD-2026-000119',
    partner: 'Công ty CP Vật tư Sài Gòn',
    issuedOn: '2026-08-07',
    amount: '3480000.50',
    status: 'waiting',
    nextAction: 'Chờ cơ quan thuế cấp mã',
  },
  {
    id: '3',
    code: 'HD-2026-000120',
    partner: 'DNTN Hải Đăng',
    issuedOn: '2026-08-11',
    amount: '870000.00',
    status: 'rejected',
    nextAction: 'Lập hóa đơn thay thế',
  },
  {
    id: '4',
    code: 'HD-2026-000121',
    partner: 'Công ty TNHH Minh Phát',
    issuedOn: '2026-08-14',
    amount: '245900000.00',
    status: 'draft',
    nextAction: 'Phát hành hóa đơn',
  },
]

/** Dòng tổng: số ĐÃ tính sẵn ở server, client không cộng lại (LD-03). */
const DEMO_TOTALS: readonly DataTableTotalRow[] = [
  {
    key: 'opening',
    label: 'Số dư đầu tháng',
    position: 'above',
    cells: { amount: '73.287.100.000' },
  },
  {
    key: 'period',
    label: 'Cộng phát sinh tháng',
    cells: { amount: '262.750.000,50' },
  },
]

const STATUS_LABEL: Record<DemoRow['status'], string> = {
  draft: 'Nháp',
  waiting: 'Chờ CQT',
  accepted: 'Đã cấp mã',
  rejected: 'Bị từ chối',
}

// Ánh xạ theo trục "có việc cho bạn không" (xem `status-pill.tsx`):
// đã cấp mã = xong = XÁM, chờ CQT = còn phải theo dõi = xanh, bị từ chối = đỏ.
const STATUS_TONE = {
  draft: 'todo',
  waiting: 'todo',
  accepted: 'ok',
  rejected: 'bad',
} as const

const CHECKLIST_ITEMS: readonly ChecklistItem[] = [
  {
    id: 'posted',
    label: 'Mọi chứng từ trong kỳ đã ghi sổ',
    status: 'passed',
    statusLabel: 'Đạt',
  },
  {
    id: 'balance',
    label: 'Tổng Nợ bằng tổng Có trên sổ cái',
    status: 'passed',
    statusLabel: 'Đạt',
  },
  {
    id: 'stock',
    label: 'Đã tính giá xuất kho cho kỳ',
    status: 'pending',
    statusLabel: 'Đang chạy',
    detail: 'Tác vụ nền đang xử lý 1 240 dòng.',
  },
  {
    id: 'invoices',
    label: 'Không còn hóa đơn điện tử chờ phát hành',
    status: 'failed',
    statusLabel: 'Còn 3',
    detail: 'HD-2026-000119, HD-2026-000120, HD-2026-000121.',
    fixLabel: 'Xem danh sách',
    onFix: () => undefined,
  },
]

interface GridLine {
  readonly id: string
  readonly item: string
  readonly quantity: string
  readonly unitPrice: string
  readonly amount: string
}

const GRID_COLUMNS: readonly DataGridColumn<GridLine>[] = [
  { key: 'item', header: 'Hàng hóa, dịch vụ', value: (row) => row.item },
  {
    key: 'quantity',
    header: 'Số lượng',
    width: 100,
    align: 'right',
    inputMode: 'decimal',
    value: (row) => row.quantity,
  },
  {
    key: 'unitPrice',
    header: 'Đơn giá',
    width: 130,
    align: 'right',
    inputMode: 'decimal',
    value: (row) => row.unitPrice,
  },
  // Ô nền xám = server tính. Lưới không nhân số lượng với đơn giá (H15).
  {
    key: 'amount',
    header: 'Thành tiền',
    width: 130,
    align: 'right',
    readOnly: true,
    value: (row) => row.amount,
    display: (row) => formatMoney(row.amount, 'vi'),
  },
]

/** Hai dòng trống cuối là có chủ đích: chứng từ luôn mở sẵn chỗ gõ tiếp. */
const GRID_LINES: readonly GridLine[] = [
  {
    id: 'g1',
    item: 'Bút bi Thiên Long TL-027',
    quantity: '100',
    unitPrice: '4500',
    amount: '450000',
  },
  {
    id: 'g2',
    item: 'Giấy A4 Double A 70gsm',
    quantity: '20',
    unitPrice: '78000',
    amount: '1560000',
  },
  { id: 'g3', item: 'Mực in Canon 328', quantity: '3', unitPrice: '425000', amount: '1275000' },
  { id: 'g4', item: '', quantity: '', unitPrice: '', amount: '0' },
  { id: 'g5', item: '', quantity: '', unitPrice: '', amount: '0' },
]

function Section({
  title,
  note,
  children,
}: {
  readonly title: string
  readonly note?: string
  readonly children: ReactElement | readonly ReactElement[]
}): ReactElement {
  return (
    <section className="rounded border border-border-default bg-background p-4">
      <h2 className="text-sm font-semibold text-primary">{title}</h2>
      {note !== undefined && <p className="mt-1 mb-3 text-xs text-text-muted">{note}</p>}
      <div className="mt-3 flex flex-col gap-3">{children}</div>
    </section>
  )
}

export function KitchenSinkPage(): ReactElement {
  const { preference, setPreference } = useTheme()
  const [tab, setTab] = useState('all')
  const [sort, setSort] = useState<DataTableSort>({ key: 'issuedOn', direction: 'desc' })
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [declared, setDeclared] = useState('no')
  const [gridLines, setGridLines] = useState<readonly GridLine[]>(GRID_LINES)

  const columns: readonly DataTableColumn<DemoRow>[] = [
    { key: 'code', header: 'Số chứng từ', sortable: true, render: (row) => row.code },
    { key: 'partner', header: 'Đối tác', render: (row) => row.partner },
    {
      key: 'issuedOn',
      header: 'Ngày lập',
      sortable: true,
      render: (row) => formatDate(row.issuedOn, 'vi'),
    },
    {
      key: 'amount',
      header: 'Thành tiền',
      align: 'right',
      sortable: true,
      render: (row) => formatMoney(row.amount, 'vi'),
    },
    {
      key: 'status',
      header: 'Trạng thái',
      render: (row) => <StatusPill tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</StatusPill>,
    },
    {
      key: 'nextAction',
      header: 'Việc tiếp theo',
      render: (row) =>
        row.nextAction === null ? (
          <NextActionCell action={null} doneLabel="Đã xong" />
        ) : (
          <NextActionCell
            action={row.nextAction}
            doneLabel="Đã xong"
            actionLabel="Làm ngay"
            onAction={() => {
              setDrawerOpen(true)
            }}
          />
        ),
    },
  ]

  return (
    <div className="min-h-screen bg-screen p-6 text-text-default">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-primary">Design system — trang duyệt</h1>
          <p className="text-xs text-text-muted">
            Chỉ có ở bản dev. Đổi chế độ sáng/tối bên phải để soi cả hai bảng màu.
          </p>
        </div>
        <SelectField
          label="Chế độ hiển thị"
          value={preference}
          onChange={(event) => {
            setPreference(event.target.value as typeof preference)
          }}
          options={THEME_PREFERENCES.map((value) => ({
            value,
            label: value === 'system' ? 'Theo hệ thống' : value === 'light' ? 'Sáng' : 'Tối',
          }))}
        />
      </header>

      <div className="flex flex-col gap-4">
        <Section title="Button" note="Ba biến thể, không hơn.">
          <div className="flex flex-wrap items-center gap-3">
            <Button>Ghi sổ</Button>
            <Button variant="secondary">Lưu nháp</Button>
            <Button variant="ghost">Hủy</Button>
            <Button disabled>Không dùng được</Button>
          </div>
        </Section>

        <Section title="TextField · SelectField" note="Nhãn luôn là <label> thật, không phải placeholder.">
          <div className="grid gap-3 md:grid-cols-3">
            <TextField label="Số chứng từ" defaultValue="HD-2026-000118" />
            <TextField label="Mã số thuế" hint="10 hoặc 13 chữ số." defaultValue="0301234567" />
            <TextField label="Ngày lập" error="Ngày lập nằm ngoài kỳ đang mở." defaultValue="2025-12-31" />
            <SelectField
              label="Hệ thống sổ"
              options={[
                { value: 'financial', label: 'Sổ tài chính' },
                { value: 'management', label: 'Sổ quản trị' },
              ]}
            />
          </div>
        </Section>

        <Section title="Alert">
          <Alert tone="error">Kỳ kế toán đã khóa — không ghi sổ được vào ngày này.</Alert>
          <Alert tone="warning">Bản client cũ hơn máy chủ: chỉ đọc được, không lưu được.</Alert>
          <Alert tone="info">Tỷ giá ngày 14/08/2026 lấy theo ngân hàng giao dịch.</Alert>
        </Section>

        <Section title="StatusPill" note="Ba tông, chấm vuông + chữ. Việc đã xong là XÁM để việc còn lại nổi lên.">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone="ok">Đã cấp mã</StatusPill>
            <StatusPill tone="ok">Đã ghi sổ</StatusPill>
            <StatusPill tone="todo">Chờ phát hành</StatusPill>
            <StatusPill tone="todo">Dưới tồn tối thiểu</StatusPill>
            <StatusPill tone="bad">Bị từ chối · sai MST</StatusPill>
            <StatusPill tone="bad">Lệch 31.600.000</StatusPill>
          </div>
        </Section>

        <Section
          title="Seg"
          note="Chọn MỘT giá trị trên form/bộ lọc — khác Tabs (đổi cả nội dung khung dưới)."
        >
          <div className="flex flex-wrap gap-4">
            <Seg
              label="Tình trạng kê khai"
              value={declared}
              onChange={setDeclared}
              options={[
                { value: 'no', label: 'Chưa kê khai' },
                { value: 'yes', label: 'Đã kê khai' },
              ]}
            />
            <Seg
              label="Hình thức thanh toán"
              value="later"
              onChange={() => undefined}
              options={[
                { value: 'later', label: 'Trả sau' },
                { value: 'now', label: 'Trả ngay' },
              ]}
            />
          </div>
        </Section>

        <Section
          title="Tabs + DataTable + NextActionCell"
          note="Tab nêu việc còn thiếu kèm số lượng; mũi tên di chuyển focus, Enter mới đổi tab."
        >
          <Tabs
            label="Lọc theo trạng thái"
            activeId={tab}
            onChange={setTab}
            tabs={[
              { id: 'all', label: 'Tất cả', count: DEMO_ROWS.length },
              { id: 'todo', label: 'Còn việc', count: 3 },
              { id: 'rejected', label: 'Bị từ chối', count: 1 },
              { id: 'empty', label: 'Kỳ trước', count: 0 },
            ]}
          >
            <DataTable
              caption="Hóa đơn bán ra trong kỳ"
              columns={columns}
              rows={tab === 'empty' ? [] : DEMO_ROWS}
              rowKey={(row) => row.id}
              emptyLabel="Chưa có chứng từ nào trong kỳ này."
              totals={tab === 'empty' ? [] : DEMO_TOTALS}
              zebra
              sort={sort}
              onSortChange={setSort}
            />
          </Tabs>
        </Section>

        <Section title="ChecklistPanel" note="Mỗi mục hỏng dẫn thẳng tới chỗ sửa.">
          <ChecklistPanel
            title="Điều kiện khóa sổ tháng 08/2026"
            summary="Chưa đủ điều kiện — còn 1 mục chưa đạt."
            items={CHECKLIST_ITEMS}
          />
        </Section>

        <Section
          title="SplitPane"
          note="Kéo bằng chuột hoặc bằng mũi tên trái/phải khi thanh chia đang được focus."
        >
          <div className="h-64 rounded border border-border-default">
            <SplitPane
              separatorLabel="Kéo để đổi bề rộng hai khung"
              storageKey="kitchen-sink"
              left={
                <div className="h-full bg-surface p-4 text-sm">
                  <p className="font-semibold text-primary">Sao kê ngân hàng</p>
                  <p className="mt-2 text-text-muted">4 giao dịch chưa khớp.</p>
                </div>
              }
              right={
                <div className="h-full p-4 text-sm">
                  <p className="font-semibold text-primary">Sổ kế toán</p>
                  <p className="mt-2 text-text-muted">6 chứng từ chưa khớp.</p>
                </div>
              }
            />
          </div>
        </Section>

        <Section title="Drawer" note="Esc đóng; bấm ra nền KHÔNG đóng (tránh mất dữ liệu đang gõ dở).">
          <div>
            <Button
              onClick={() => {
                setDrawerOpen(true)
              }}
            >
              Mở panel
            </Button>
          </div>
        </Section>

        <Section
          title="DataGrid"
          note="Lưới NHẬP LIỆU (khác DataTable chỉ-đọc). Tab/Enter đi ô, mũi tên ngang chỉ nhảy ô khi con trỏ đã ở mép, dán được cả vùng từ Excel. Đo hiệu năng ở /bench/data-grid."
        >
          <DataGrid
            columns={GRID_COLUMNS}
            rows={gridLines}
            rowKey={(row) => row.id}
            caption="Chi tiết chứng từ bán hàng"
            cellLabel={(header, rowNumber) => `${header}, dòng ${rowNumber}`}
            rowHeight={30}
            height={150}
            onCommit={(changes) => {
              setGridLines((previous) =>
                previous.map((line, index) => {
                  const change = changes.find((item) => item.rowIndex === index)
                  return change === undefined ? line : { ...line, [change.columnKey]: change.value }
                }),
              )
            }}
          />
        </Section>
      </div>

      <Drawer
        open={drawerOpen}
        title="HD-2026-000119"
        closeLabel="Đóng"
        onClose={() => {
          setDrawerOpen(false)
        }}
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setDrawerOpen(false)
              }}
            >
              Hủy
            </Button>
            <Button>Lưu</Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <TextField label="Đối tác" defaultValue="Công ty CP Vật tư Sài Gòn" />
          <TextField label="Diễn giải" defaultValue="Bán vật tư theo hợp đồng 118/2026" />
          <Alert tone="warning">Hóa đơn đang chờ cơ quan thuế cấp mã.</Alert>
        </div>
      </Drawer>
    </div>
  )
}
