export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const configuredBase = typeof window !== "undefined"
  ? window.__CLUSTERX_MONITOR_CONFIG__?.apiBaseUrl
  : "";
const apiBase = (configuredBase ?? "").trim().replace(/\/$/, "");
export const monitorApiUrl = (path: string) => `${apiBase}/api/v1${path}`;

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(monitorApiUrl(path), { credentials: "include", ...init });
  if (!response.ok) {
    let message = response.statusText;
    try {
      message = (await response.json()).detail ?? message;
    } catch {
      // Some proxies return a non-JSON error page.
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

export const errorMessage = (value: unknown) => value instanceof Error ? value.message : String(value);
