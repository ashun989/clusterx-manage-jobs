import { beforeEach, describe, expect, it } from "vitest";

import { parseAfsCatalog, readAfsCatalog } from "../src/afs-catalog";

const requestedId = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";

describe("AFS catalog", () => {
  beforeEach(() => {
    document.body.replaceChildren();
  });

  it("extracts only requested UUID-to-name mappings from the split table body", () => {
    document.body.innerHTML = `
      <table><thead><tr><th>名称/显示名称</th><th>资源UUID</th></tr></thead></table>
      <table><tbody>
        <tr><td><div class="showNameItem-highlight">afs-team-models</div><span>--</span></td><td>${requestedId}</td></tr>
        <tr><td><span title="afs-team-archive">afs-team-archive</span></td><td>${otherId}</td></tr>
      </tbody></table>
    `;

    expect(parseAfsCatalog(document, [requestedId])).toEqual({
      [requestedId]: "afs-team-models",
    });
  });

  it("waits for dynamically rendered rows", async () => {
    document.body.innerHTML = "<table><tbody></tbody></table><span>共 0 条</span>";
    const result = readAfsCatalog(document, [requestedId], 1_000);
    document.querySelector("tbody")!.innerHTML = `
      <tr><td title="afs-team-models">afs-team-models</td><td>${requestedId}</td></tr>
    `;

    await expect(result).resolves.toEqual({
      [requestedId]: "afs-team-models",
    });
  });
});
