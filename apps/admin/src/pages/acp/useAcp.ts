/** ACP 服务页面共用的查询：接入配置与租户下拉。 */
import { useQuery } from '@tanstack/react-query';
import { api, unwrap } from '@/api';
import type { AcpConfig, AcpTenant, PagedResp } from '@/types';

export function useAcpConfig() {
  return useQuery({
    queryKey: ['acp-config'],
    queryFn: () => unwrap<AcpConfig>(api.GET('/api/v1/admin/acp-gateway/config')),
  });
}

/** 租户下拉用（网关租户数很少，一次取满 200）。 */
export function useAcpTenantOptions() {
  return useQuery({
    queryKey: ['acp-tenants', 'options'],
    queryFn: () =>
      unwrap<PagedResp<AcpTenant>>(
        api.GET('/api/v1/admin/acp-gateway/tenants', { params: { query: { limit: 200 } } }),
      ),
  });
}
