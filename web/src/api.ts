export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, init);
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
