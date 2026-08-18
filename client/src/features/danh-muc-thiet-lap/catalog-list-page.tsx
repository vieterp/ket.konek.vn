/**
 * Màn hình danh mục — cây bên trái, lưới bên phải (bước 19, nhóm 07).
 *
 * Cây chỉ hiện **nhóm**: nó là bộ lọc "đang đứng ở nhánh nào", còn nội dung
 * nhánh (cả nhóm con lẫn bản ghi) nằm ở lưới — hiện cả hai bên thì mỗi bản ghi
 * xuất hiện hai lần trên một màn hình. Lưới đọc `children_of(nút đang chọn)`
 * có phân trang (H52); bấm một dòng nhóm là đi xuống nhánh đó.
 *
 * Không có ô tìm kiếm: hợp đồng danh sách phía server chưa có tham số tìm
 * (3B-1 giao `parent_id`/`subtree_of`/`flag`/phân trang). Thêm tìm kiếm là
 * việc của server trước — ghi ở phần nợ của lát này, không độ chế client-side
 * để rồi chỉ tìm thấy trang đang tải.
 */

import type { ReactElement } from 'react'
import { useCallback, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { FolderClosed, Pencil } from 'lucide-react'

import type { DataTableColumn, TreePickerNode } from '@/design-system/components'
import { Alert, Button, DataTable, Seg, SplitPane, StatusPill, TreePicker } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { CatalogEditDrawer } from './catalog-edit-drawer'
import { catalogByUrlSegment } from './catalog-registry'
import type { CatalogDef, CatalogRow } from './catalog-types'
import { extraValue } from './catalog-types'
import { FeatureNav } from './feature-nav'
import { ImportWizard } from './import-wizard'
import { useLoadChildren } from './use-load-children'
import { PAGE_SIZE, useCatalogPage } from './use-catalog'
import { useDownloadTemplate, useExportCatalog } from './use-import'

const ALL_FLAG = ''

export function CatalogListPage(): ReactElement {
  const { segment } = useParams()
  const catalog = catalogByUrlSegment(segment ?? '')

  if (catalog === undefined) {
    // Đường dẫn lạ (dấu trang cũ, gõ tay) — về danh mục đầu tiên thay vì trang trắng.
    return <Navigate to="/danh-muc-thiet-lap/danh-muc/doi-tac" replace />
  }
  // `key` bảo đảm mọi state trong `CatalogContent` khởi tạo lại khi đổi danh mục.
  return <CatalogContent key={catalog.slug} def={catalog} />
}

function CatalogContent({ def }: { readonly def: CatalogDef }): ReactElement {
  const { t } = useI18n()
  const navigate = useNavigate()
  const { readOnly } = useSession()

  const [selectedNodeId, setSelectedNodeId] = useState<number | null>(null)
  const [flag, setFlag] = useState<string>(ALL_FLAG)
  const [page, setPage] = useState(0)
  const [drawer, setDrawer] = useState<{ readonly open: boolean; readonly record: CatalogRow | null }>({
    open: false,
    record: null,
  })
  const [wizardOpen, setWizardOpen] = useState(false)
  // Cây nạp lười bằng callback (không phải truy vấn có đệm), nên sau một lệnh
  // ghi nó không tự mới lại — tăng số này để dựng lại cây (mất trạng thái mở
  // nhánh, đổi lấy việc nhóm vừa tạo/xóa hiện đúng — review 3D M-1).
  const [treeVersion, setTreeVersion] = useState(0)

  const listQuery = useCatalogPage(def.slug, {
    parentId: selectedNodeId,
    flag: flag === ALL_FLAG ? null : flag,
    page,
  })
  const loadChildren = useLoadChildren(def.slug)
  const loadGroupChildren = useCallback(
    async (parentId: number | null): Promise<readonly TreePickerNode[]> => {
      const children = await loadChildren(parentId)
      return children.filter((node) => node.isGroup)
    },
    [loadChildren],
  )

  const download = useDownloadTemplate(def.slug)
  const exporter = useExportCatalog(def.slug)

  const rows = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const columns: DataTableColumn<CatalogRow>[] = [
    {
      key: 'code',
      header: t('catalog.field.code'),
      render: (row) =>
        row.is_group ? (
          <button
            type="button"
            className="flex items-center gap-1 font-semibold text-primary hover:underline"
            onClick={() => {
              setSelectedNodeId(row.id)
              setPage(0)
            }}
          >
            <FolderClosed size={14} aria-hidden />
            {row.code}
          </button>
        ) : def.slug === 'partners' ? (
          <button
            type="button"
            className="text-secondary hover:underline"
            onClick={() => {
              void navigate(`/danh-muc-thiet-lap/doi-tac/${String(row.id)}`)
            }}
          >
            {row.code}
          </button>
        ) : (
          row.code
        ),
    },
    { key: 'name', header: t('catalog.field.name'), render: (row) => row.name },
    ...def.listColumns.map((key) => {
      const field = def.extraFields.find((candidate) => candidate.key === key)
      return {
        key,
        header: field === undefined ? key : t(field.labelKey),
        render: (row: CatalogRow) => {
          const value = extraValue(row, key)
          if (value === null || value === false) {
            return ''
          }
          if (value === true) {
            return '✓'
          }
          if (field?.type === 'select') {
            const option = field.options?.find((candidate) => candidate.value === value)
            return option === undefined ? String(value) : t(option.labelKey)
          }
          return String(value)
        },
      }
    }),
    {
      key: 'status',
      header: t('catalog.column.status'),
      render: (row) =>
        row.is_active ? null : <StatusPill tone="todo">{t('catalog.status.inactive')}</StatusPill>,
    },
    {
      key: 'actions',
      header: '',
      align: 'right',
      render: (row) => (
        <Button
          variant="ghost"
          aria-label={t('catalog.action.edit', { code: row.code })}
          onClick={() => {
            setDrawer({ open: true, record: row })
          }}
        >
          <Pencil size={14} aria-hidden />
        </Button>
      ),
    },
  ]

  const listError =
    listQuery.error instanceof ApiError
      ? translateErrorCode(t, listQuery.error.errorCode)
      : listQuery.isError
        ? t('error.transport.unreachable')
        : null

  // Tải tệp thất bại phải nói ra — nút bấm mà không có gì xảy ra là loại lỗi
  // người dùng không thể tự chẩn đoán (review 3D M-3).
  const downloadError = [download.error, exporter.error]
    .map((caught) =>
      caught instanceof ApiError ? translateErrorCode(t, caught.errorCode) : null,
    )
    .find((message) => message !== null) ??
    (download.isError || exporter.isError ? t('error.transport.unreachable') : null)

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-lg font-semibold text-primary">{t(def.titleKey)}</h1>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              disabled={download.isPending}
              onClick={() => {
                download.mutate()
              }}
            >
              {t('catalog.action.downloadTemplate')}
            </Button>
            <Button
              variant="secondary"
              disabled={exporter.isPending}
              onClick={() => {
                exporter.mutate({ includeInactive: false })
              }}
            >
              {t('catalog.action.export')}
            </Button>
            {!readOnly && (
              <Button
                variant="secondary"
                onClick={() => {
                  setWizardOpen(true)
                }}
              >
                {t('catalog.action.import')}
              </Button>
            )}
            {!readOnly && (
              <Button
                onClick={() => {
                  setDrawer({ open: true, record: null })
                }}
              >
                {t('catalog.action.create')}
              </Button>
            )}
          </div>
        </header>

        {def.flags.length > 0 && (
          <Seg
            label={t('catalog.flag.label')}
            value={flag}
            onChange={(value) => {
              setFlag(value)
              setPage(0)
            }}
            options={[
              { value: ALL_FLAG, label: t('catalog.flag.all') },
              ...def.flags.map((item) => ({ value: item.value, label: t(item.labelKey) })),
            ]}
          />
        )}

        {listError !== null && <Alert tone="error">{listError}</Alert>}
        {downloadError !== null && <Alert tone="error">{downloadError}</Alert>}

        <div className="min-h-0 flex-1">
          <SplitPane
            separatorLabel={t('catalog.tree.separator')}
            initialPercent={28}
            minPercent={16}
            maxPercent={50}
            storageKey={`ket.catalog-split.${def.slug}`}
            left={
              <TreePicker
                key={treeVersion}
                label={t('catalog.tree.label')}
                selectedId={selectedNodeId}
                onSelect={(node) => {
                  setSelectedNodeId(node?.id ?? null)
                  setPage(0)
                }}
                loadChildren={loadGroupChildren}
                selectableGroups
                rootLabel={t('catalog.tree.root')}
                loadingLabel={t('common.loading')}
                emptyLabel={t('catalog.tree.empty')}
                errorLabel={t('error.transport.unreachable')}
              />
            }
            right={
              <div className="flex h-full flex-col gap-2">
                <DataTable
                  caption={t(def.titleKey)}
                  columns={columns}
                  rows={rows}
                  rowKey={(row) => String(row.id)}
                  emptyLabel={t('catalog.list.empty')}
                  loading={listQuery.isPending}
                  loadingLabel={t('common.loading')}
                  zebra
                />
                <footer className="flex items-center justify-between text-xs text-text-muted">
                  <span>
                    {t('catalog.list.pageInfo', {
                      from: String(total === 0 ? 0 : page * PAGE_SIZE + 1),
                      to: String(Math.min(total, (page + 1) * PAGE_SIZE)),
                      total: String(total),
                    })}
                  </span>
                  <span className="flex gap-2">
                    <Button
                      variant="ghost"
                      disabled={page === 0}
                      onClick={() => {
                        setPage((current) => Math.max(0, current - 1))
                      }}
                    >
                      {t('catalog.list.prev')}
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={page >= pageCount - 1}
                      onClick={() => {
                        setPage((current) => current + 1)
                      }}
                    >
                      {t('catalog.list.next')}
                    </Button>
                  </span>
                </footer>
              </div>
            }
          />
        </div>
      </section>

      <CatalogEditDrawer
        catalog={def}
        record={drawer.record}
        open={drawer.open}
        initialParentId={selectedNodeId}
        onClose={() => {
          setDrawer({ open: false, record: null })
        }}
        onSaved={() => {
          setDrawer({ open: false, record: null })
          setTreeVersion((version) => version + 1)
        }}
      />

      <ImportWizard
        catalog={def}
        open={wizardOpen}
        onClose={() => {
          setWizardOpen(false)
        }}
      />
    </div>
  )
}
