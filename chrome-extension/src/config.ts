import { load } from "js-yaml";

import type {
  AfsMount,
  AossMount,
  ResolvedClusterxProfile,
  ResolvedMount,
} from "./types";

const MAX_CONFIG_BYTES = 1024 * 1024;
const FORBIDDEN_MOUNT_ROOTS = [
  "/bin",
  "/boot",
  "/dev",
  "/etc",
  "/lib",
  "/lib64",
  "/proc",
  "/root",
  "/run",
  "/sbin",
  "/sys",
  "/usr",
  "/var",
];

type RecordValue = Record<string, unknown>;

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

function record(value: unknown, path: string): RecordValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ConfigError(`${path} 必须是对象`);
  }
  return value as RecordValue;
}

function requiredString(source: RecordValue, key: string, path: string): string {
  const value = source[key];
  if (typeof value !== "string" || value.trim() === "") {
    throw new ConfigError(`${path}.${key} 必须是非空字符串`);
  }
  return value.trim();
}

function optionalString(source: RecordValue, key: string, path: string): string | undefined {
  const value = source[key];
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value !== "string") {
    throw new ConfigError(`${path}.${key} 必须是字符串`);
  }
  return value.trim() || undefined;
}

function validateSubdir(value: string | undefined, path: string): string | undefined {
  if (!value) return undefined;
  if (value.startsWith("/") || value.split("/").includes("..")) {
    throw new ConfigError(`${path} 必须是相对路径且不能包含 ..`);
  }
  return value.replace(/^\.\//, "").replace(/\/$/, "");
}

function validateMountPath(value: string, path: string): string {
  if (!value.startsWith("/") || value.includes("\0") || value.split("/").includes("..")) {
    throw new ConfigError(`${path} 必须是无 .. 的绝对路径`);
  }
  const normalized = value.length > 1 ? value.replace(/\/$/, "") : value;
  if (
    normalized === "/" ||
    FORBIDDEN_MOUNT_ROOTS.some(
      (root) => normalized === root || normalized.startsWith(`${root}/`),
    )
  ) {
    throw new ConfigError(`${path} 不能位于系统目录`);
  }
  return normalized;
}

function metadataCredentials(source: RecordValue, path: string): [string, string] {
  const metadata = record(source.metadata, `${path}.metadata`);
  if (!Array.isArray(metadata.items)) {
    throw new ConfigError(`${path}.metadata.items 必须是数组`);
  }
  const values = new Map<string, string>();
  metadata.items.forEach((entry, index) => {
    const item = record(entry, `${path}.metadata.items[${index}]`);
    const key = requiredString(item, "key", `${path}.metadata.items[${index}]`);
    const value = requiredString(item, "value", `${path}.metadata.items[${index}]`);
    values.set(key, value);
  });
  const accessKey = values.get("access_key");
  const secretKey = values.get("secret_key");
  if (!accessKey || !secretKey) {
    throw new ConfigError(`${path}.metadata.items 缺少 access_key 或 secret_key`);
  }
  return [accessKey, secretKey];
}

function parseMount(value: unknown, index: number): ResolvedMount {
  const path = `mount[${index}]`;
  const source = record(value, path);
  const type = requiredString(source, "type", path);
  const mountPath = validateMountPath(requiredString(source, "mount_path", path), `${path}.mount_path`);
  const subdir = validateSubdir(optionalString(source, "subdir", path), `${path}.subdir`);

  if (type === "PV_AFS") {
    const mount: AfsMount = {
      type,
      id: requiredString(source, "id", path),
      mountPath,
    };
    if (subdir) mount.subdir = subdir;
    return mount;
  }
  if (type === "PV_AOSS") {
    const endpoint = requiredString(source, "endpoint", path);
    let parsedEndpoint: URL;
    try {
      parsedEndpoint = new URL(endpoint);
    } catch {
      throw new ConfigError(`${path}.endpoint 必须是有效 URL`);
    }
    if (!new Set(["http:", "https:"]).has(parsedEndpoint.protocol)) {
      throw new ConfigError(`${path}.endpoint 只允许 http 或 https`);
    }
    const [accessKey, secretKey] = metadataCredentials(source, path);
    const mount: AossMount = {
      type,
      name: requiredString(source, "name", path),
      endpoint,
      mountPath,
      accessKey,
      secretKey,
    };
    if (subdir) mount.subdir = subdir;
    return mount;
  }
  throw new ConfigError(`${path}.type 仅支持 PV_AFS 或 PV_AOSS`);
}

export function parseClusterxConfig(source: string): ResolvedClusterxProfile {
  if (new TextEncoder().encode(source).byteLength > MAX_CONFIG_BYTES) {
    throw new ConfigError("配置文件不能超过 1 MiB");
  }

  let parsed: unknown;
  try {
    parsed = load(source, { json: true });
  } catch {
    throw new ConfigError("无法解析 YAML 配置");
  }

  const root = record(parsed, "配置");
  const profileName = requiredString(root, "default", "配置");
  const profile = record(root[profileName], `配置.${profileName}`);
  const rawMounts = profile.mount ?? [];
  if (!Array.isArray(rawMounts)) {
    throw new ConfigError(`配置.${profileName}.mount 必须是数组`);
  }
  const mounts = rawMounts.map(parseMount);
  const afsCount = mounts.filter((mount) => mount.type === "PV_AFS").length;
  const aossCount = mounts.filter((mount) => mount.type === "PV_AOSS").length;
  if (afsCount > 10 || aossCount > 10) {
    throw new ConfigError("PV_AFS 和 PV_AOSS 各自最多允许 10 个挂载");
  }
  const paths = new Set<string>();
  for (const mount of mounts) {
    if (paths.has(mount.mountPath)) {
      throw new ConfigError("mount 中存在重复的 mount_path");
    }
    paths.add(mount.mountPath);
  }

  return {
    profileName,
    scope: {
      subscription: requiredString(profile, "subscription", `配置.${profileName}`),
      resourceGroup: requiredString(profile, "resource_group", `配置.${profileName}`),
      region: requiredString(profile, "region", `配置.${profileName}`),
      workspace: requiredString(profile, "workspace", `配置.${profileName}`),
    },
    queue: requiredString(profile, "queue", `配置.${profileName}`),
    rdmaName: requiredString(profile, "rdma_name", `配置.${profileName}`),
    image: requiredString(profile, "image", `配置.${profileName}`),
    mounts,
  };
}
