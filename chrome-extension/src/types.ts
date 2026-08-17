export interface ClusterxScope {
  subscription: string;
  resourceGroup: string;
  region: string;
  workspace: string;
}

export interface AfsMount {
  type: "PV_AFS";
  id: string;
  mountPath: string;
  subdir?: string;
}

export interface AossMount {
  type: "PV_AOSS";
  name: string;
  endpoint: string;
  mountPath: string;
  subdir?: string;
  accessKey: string;
  secretKey: string;
}

export type ResolvedMount = AfsMount | AossMount;

export interface ResolvedClusterxProfile {
  profileName: string;
  scope: ClusterxScope;
  queue: string;
  rdmaName: string;
  image: string;
  mounts: ResolvedMount[];
}

export interface FillRequest {
  type: "clusterx.fill-development";
  profile: ResolvedClusterxProfile;
  afsCatalog?: Record<string, string>;
}

export interface AfsCatalogRequest {
  type: "clusterx.read-afs-catalog";
  ids: string[];
}

export interface AfsCatalogResponse {
  ok: boolean;
  catalog: Record<string, string>;
}

export type FillItemStatus = "filled" | "skipped" | "warning" | "error";

export interface FillItemResult {
  key: string;
  label: string;
  status: FillItemStatus;
  message: string;
}

export interface FillReport {
  ok: boolean;
  fatal: boolean;
  items: FillItemResult[];
}

export function isFillRequest(value: unknown): value is FillRequest {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<FillRequest>;
  return candidate.type === "clusterx.fill-development" && !!candidate.profile;
}

export function isAfsCatalogRequest(value: unknown): value is AfsCatalogRequest {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AfsCatalogRequest>;
  return candidate.type === "clusterx.read-afs-catalog"
    && Array.isArray(candidate.ids)
    && candidate.ids.length <= 10
    && candidate.ids.every((id) => typeof id === "string" && id.length > 0);
}

export function scrubProfileSecrets(profile: ResolvedClusterxProfile): void {
  for (const mount of profile.mounts) {
    if (mount.type === "PV_AOSS") {
      mount.accessKey = "";
      mount.secretKey = "";
    }
  }
}
