# Clusterx Manage Jobs with Monitor

本项目为 PT/SSP 集群提供两部分能力：通过 Codex Skill 安全管理 Clusterx
训练任务，以及通过 Monitor 只读观察一个配置 queue 的资源、任务和节点状态。
Monitor 不创建、停止或修改 Clusterx 任务；任务生命周期仍由 Clusterx wrapper
负责。

## 组件与文档

| 组件 | 内容 | 文档 |
| --- | --- | --- |
| Server | Monitor API、采集器、策略和调度模拟 | [server/README.md](server/README.md) |
| Client | Monitor CLI、Clusterx wrapper、preflight 和脱敏工具 | [client/README.md](client/README.md) |
| Web | 监控面板和管理员配置界面 | [web/README.md](web/README.md) |
| Skill | Codex 使用规则、Clusterx 参考和配置指导 | [skills/clusterx-manage-jobs/SKILL.md](skills/clusterx-manage-jobs/SKILL.md) |
| Deployment | Docker、systemd 和跨组件部署 | [deploy/README.md](deploy/README.md) |
| Chrome extension | 开发机创建页配置填充 | [chrome-extension/README.md](chrome-extension/README.md) |
| Smoke projects | 集群资源和存储验证项目 | [smoke-projects/README.md](smoke-projects/README.md) |

跨组件开发和发布流程见 [docs/development.md](docs/development.md) 与
[docs/release.md](docs/release.md)。历史问题记录位于 [docs/](docs/)。

## 布局

```text
server/                         Monitor Server Python package
client/                         Monitor Client Python package
web/                            React/TypeScript dashboard
skills/clusterx-manage-jobs/    Codex Skill package
deploy/                         Docker and systemd templates
config/                         example and local ignored configuration
scripts/                        release and maintenance helpers
tmp/                            ignored local build and deployment helpers
tests/                          simulated-gateway tests
smoke-projects/                 optional cluster validation projects
chrome-extension/               development-page browser extension
```

Server、Web、Client 和 Skill 各自维护自己的 `VERSION` 文件；组件 README 不复制
具体版本号。使用 `scripts/check_versions.py` 检查 VERSION、包元数据和 Web 元数据。

## 项目边界

- Server 当前只服务 Clusterx 配置选定的一个 queue。
- Monitor 只读观察和分析资源与任务；任务生命周期由 Clusterx wrapper 负责。
- 真实 Clusterx 配置、管理员配置、私有分组和本地策略属于本机部署数据，不得提交。

节点分配、quota 和 CLI 身份规则请以 [Server](server/README.md)、[Client](client/README.md)、
[Web](web/README.md) 及 [Skill](skills/clusterx-manage-jobs/SKILL.md) 文档为准。

## 快速开始

组件安装、配置和运行命令请使用上表中的组件文档。仓库级验证命令如下：

```bash
pytest -q
(cd web && npm test -- --run && npm run build)
(cd chrome-extension && npm test && npm run build)
python3 scripts/check_versions.py
```

发布四个独立组件请参阅 [docs/release.md](docs/release.md)。
