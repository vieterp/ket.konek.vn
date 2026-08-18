/**
 * Dữ liệu màn hình Thiết lập.
 *
 * BFF `/setup/settings-groups` phân nhóm theo **hệ quả** (U14) và là đọc-only;
 * sửa một tùy chọn vẫn đi qua `/system/settings/{key}` — đúng quy ước RT-21
 * "BFF không ghi". Vì `SetupItem` của BFF không mang `row_version`, màn hình
 * sửa phải đọc thêm danh sách tùy chọn gốc để lấy phiên bản dòng và kiểu giá
 * trị; hai truy vấn được ghép ở đây để trang chỉ thấy một hình dạng dữ liệu.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type SetupGroup = Schemas['SetupGroup']
export type SetupItem = Schemas['SetupItem']
export type Setting = Schemas['SettingResponse']

export function useSettingsGroups() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['settings-groups', datasetCode],
    enabled: datasetCode !== null,
    queryFn: async () => {
      const response = await client.get<Schemas['SettingsGroupsResponse']>(
        '/api/v1/setup/settings-groups',
        { datasetCode },
      )
      return response.groups
    },
  })
}

/** Danh sách tùy chọn gốc — nguồn `row_version`/`value_type`/`scopes` cho việc sửa. */
export function useSettings() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['settings', datasetCode],
    enabled: datasetCode !== null,
    queryFn: async () => {
      const response = await client.get<Schemas['SettingListResponse']>(
        '/api/v1/system/settings',
        { datasetCode },
      )
      return response.items
    },
  })
}

export function useUpdateSetting() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      key,
      value,
      scope,
      rowVersion,
    }: {
      readonly key: string
      readonly value: string
      readonly scope: string
      /** `null` = tùy chọn chưa có dòng nào ở cấp này — hợp đồng của server. */
      readonly rowVersion: number | null
    }) =>
      client.put<Setting>(
        `/api/v1/system/settings/${key}`,
        { value, scope, row_version: rowVersion },
        { datasetCode },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['settings', datasetCode] })
      void queryClient.invalidateQueries({ queryKey: ['settings-groups', datasetCode] })
    },
  })
}
