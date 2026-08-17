import { describe, expect, it } from "vitest";

import { expectedWorkspaceId, validateDevelopmentUrl } from "../src/scope";

const scope = {
  subscription: "sub-example",
  resourceGroup: "default",
  region: "cn-test-01",
  workspace: "ws-example",
};

function createUrl(workspaceId = expectedWorkspaceId(scope), region = scope.region): string {
  return `https://console.d.pjlab.org.cn/${region}/ssp/model/development/create?workspaceId=${encodeURIComponent(workspaceId)}`;
}

describe("validateDevelopmentUrl", () => {
  it("accepts the exact development page scope", () => {
    expect(validateDevelopmentUrl(createUrl(), scope)).toBeNull();
  });

  it("rejects region, workspace, host, and page mismatches", () => {
    expect(validateDevelopmentUrl(createUrl(undefined, "cn-other"), scope)).toMatch(/地域/);
    expect(validateDevelopmentUrl(createUrl("/subscriptions/other"), scope)).toMatch(/工作空间/);
    expect(validateDevelopmentUrl("https://example.test/create", scope)).toMatch(/控制台/);
    expect(validateDevelopmentUrl("https://console.d.pjlab.org.cn/cn-test-01/home", scope)).toMatch(/创建页/);
  });
});
