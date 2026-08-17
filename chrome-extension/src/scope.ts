import type { ClusterxScope } from "./types";

export function expectedWorkspaceId(scope: ClusterxScope): string {
  return `/subscriptions/${scope.subscription}/resourceGroups/${scope.resourceGroup}/regions/${scope.region}/workspaces/${scope.workspace}`;
}

export function validateDevelopmentUrl(rawUrl: string, scope: ClusterxScope): string | null {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    return "当前页面 URL 无效";
  }
  if (url.protocol !== "https:" || url.hostname !== "console.d.pjlab.org.cn") {
    return "当前页面不是受支持的 ClusterX 控制台";
  }
  const match = url.pathname.match(/^\/([^/]+)\/ssp\/model\/development\/create\/?$/);
  if (!match) return "当前页面不是开发机创建页";
  if (decodeURIComponent(match[1]) !== scope.region) return "页面地域与配置不一致";
  if (url.searchParams.get("workspaceId") !== expectedWorkspaceId(scope)) {
    return "页面工作空间与配置不一致";
  }
  return null;
}
