# Clusterx Manage Jobs Skill with Monitor

用于安全管理 PT/SSP 集群 Clusterx 训练任务，并提供新增的只读队列监控、
资源策略检查和调度模拟。当前版本为 `0.3.0`，已验证 Clusterx `2026.8.11`；
其他版本以安装后的动态帮助为准。

原有任务生命周期能力保持不变：配置检查、提交预览与创建、任务/节点查询、
日志、统计、精确目标停止、挂载与凭据脱敏均通过 Clusterx Skill 完成。
`clusterx_monitor` 只增加监控能力，不接管或依赖这些 CRUD 操作。

## 架构

```text
Clusterx SDK → clusterx-monitor → immutable snapshots → Web / Skill CLI
                                             └──────→ scheduling simulator
```

- `src/clusterx_monitor`：集中采集、策略、缓存、API 和求解器。
- `web`：React/TypeScript 监控面板。
- `skill/clusterx-manage-jobs/assets/resource-policy.json`：只读的公共资源规则模板。
- `config/resource-policy.local.json`：权限为 `600`、被 Git 忽略的可写生效规则。
- `config/groups.local.yaml`：权限为 `600`、被 Git 忽略的本地私有分组配置。
- `config/admin.local.yaml`：仅保存 Argon2id 密码哈希的本地管理员配置。
- `skill/clusterx-manage-jobs`：可独立打包的 Skill；监控 CLI 只访问本机 API。
- `tests`：模拟数据测试，不连接真实集群。

监控服务对 Clusterx 完全只读，不提供停止、驱逐或自动整改接口；认证管理员只
能写入本机策略文件。任务提交和停止仍由 Skill 已有的受控 Clusterx 工作流处理。

## Skill 安装与任务生命周期

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -a skill/clusterx-manage-jobs \
  "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs"
python3 -m pip install -r \
  "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs/requirements.txt"
```

Clusterx CLI 是独立前置条件，只能在 Linux 集群开发机使用，不随 Skill 分发。
通过当前公司内部渠道安装到隔离环境，并使用统一包装器执行生命周期操作：

```bash
python3 skill/clusterx-manage-jobs/scripts/preflight.py \
  --cwd <project> --tmpdir <shared-tmpdir>
python3 skill/clusterx-manage-jobs/scripts/clusterx_exec.py \
  --cwd <project> -- list
python3 skill/clusterx-manage-jobs/scripts/clusterx_exec.py \
  --cwd <project> -- get-job <job-id> --workers
python3 skill/clusterx-manage-jobs/scripts/clusterx_exec.py \
  --cwd <project> -- log <job-id>
python3 skill/clusterx-manage-jobs/scripts/clusterx_exec.py \
  --cwd <project> -- stats --scope job --job <exact-job-name> --metric all
```

提交命令会在调用 Clusterx 前强制校验 CPU：GPU 任务满足
`CPU <= GPU × cpu_per_gpu`，0-GPU 任务满足
`CPU <= zero_gpu_max_cpu_per_node`。内置规则对应 0 GPU/14 CPU、
1 GPU/14 CPU、8 GPU/112 CPU，且节点数不放大单节点上限。完整命令、挂载、
A3、Worker、日志限制和停止规则见 Skill reference。

用户明确请求提交或停止准确目标时，Skill 按请求执行；目标含糊、参数缺失或
新增特权模式时才询问。包装器拒绝 Clusterx 无法安全保持参数边界的
`bash/sh -c/-lc`，并自动脱敏 stdout/stderr。

## Monitor 安装与启动

服务要求 Linux、Python 3.10+、Clusterx CLI 和权限为 `600` 的 Clusterx 配置。

```bash
python3 -m pip install -e .
cd web && npm install && npm run build && cd ..
cp skill/clusterx-manage-jobs/assets/resource-policy.json \
  config/resource-policy.local.json
cp config/groups.example.yaml config/groups.local.yaml
chmod 600 config/resource-policy.local.json config/groups.local.yaml
clusterx-monitor admin init \
  --auth-config config/admin.local.yaml \
  --username clusterx-admin
# 密码通过终端隐藏输入，不要放在命令参数、Git 或聊天中
clusterx-monitor serve \
  --clusterx-config ~/.config/clusterx.yaml \
  --policy-config config/resource-policy.local.json \
  --group-config config/groups.local.yaml \
  --auth-config config/admin.local.yaml \
  --host 127.0.0.1 \
  --port 8765
```

默认只监听 loopback。打开 `http://127.0.0.1:8765` 查看面板。已有受控 NAT 时可
显式监听外部接口，但必须声明可信 Host，例如：

```bash
clusterx-monitor serve \
  --clusterx-config ~/.config/clusterx.yaml \
  --policy-config config/resource-policy.local.json \
  --group-config config/groups.local.yaml \
  --auth-config config/admin.local.yaml \
  --host 0.0.0.0 --allowed-host 10.140.80.10 --port 8765
```

`--allowed-host` 不接受 `*`，可重复指定。此模式本身不提供 TLS；通过明文 HTTP
登录管理员会暴露密码与 session，应仅在受控网络临时使用，长期部署仍应使用
HTTPS 反向代理。

右上角“管理员配置”通过服务端认证修改资源策略和私有分组。密码只以 Argon2id
哈希保存在权限为 `600` 的 `config/admin.local.yaml`；session 只驻内存并通过
HttpOnly、SameSite=Strict Cookie 传递。所有写请求还要求 CSRF token、精确
Origin、JSON Content-Type 和 revision。配置先完整校验，再以 `600` 权限原子
替换本地文件，同时保留一个 `.bak` 并写入不含配置内容的审计记录。

轮换密码仍通过隐藏输入完成，不经过浏览器、argv 或环境变量；轮换会使已有会话
失效：

```bash
clusterx-monitor admin init \
  --auth-config config/admin.local.yaml \
  --username clusterx-admin --force
```

若资源或分组文件不存在，服务进入 `setup-required`：不发布普通监控快照，但
管理员仍可登录并从默认模板初始化两份配置；两者有效后自动开始采集，无需重启。
公共 `/api/v1/policy` 继续只返回分组 ID、quota 和成员数量。

服务每 30 秒串行刷新一次，跳过重叠 tick；最近 5 份完整快照保存在内存中。
采集失败时继续提供最后完整快照并标记 stale。GPU compute、显存和功率使用
最近 5 分钟滚动平均，并显示逐卡遥测覆盖率。

Workload 快照统一提供 `total_gpu`、`total_cpu`、`total_memory_gib` 和
`resource_basis`。Running 资源按 Pod placement 归属汇总；Pending 资源按全部
task 的副本申请汇总，并在 `task_resources` 中保留每个 task 的资源规格。无法
取得 CPU 或内存时返回 `null`，不会将未知资源显示为零。
Group 与 User 的已分配资源均汇总全部活跃 workload 类型（包括 `trainingJob`、
`aid` 和 `air`）；Pending 仍只展示申请量，不计入当前已分配资源。

服务另按 workload UID 从 Prometheus 聚合过去 24 小时的历史 GPU 利用率，默认
每 5 分钟刷新；该缓存只驻留内存，重启后立即重建，不使用数据库。历史查询失败
不会阻止快照和 5 分钟遥测发布，但会产生结构化遥测告警。

监控 CLI 默认访问 `http://127.0.0.1:8765`：

```bash
python3 skill/clusterx-manage-jobs/scripts/monitor_cli.py overview --format json
python3 skill/clusterx-manage-jobs/scripts/monitor_cli.py groups --violations-only
python3 skill/clusterx-manage-jobs/scripts/monitor_cli.py workloads \
  --finding-category utilization --tag low-utilization
python3 skill/clusterx-manage-jobs/scripts/monitor_cli.py nodes \
  --classification fragmented,cpu-memory-blocked
python3 skill/clusterx-manage-jobs/scripts/monitor_cli.py plan \
  --nodes 2 --gpus-per-node 8 \
  --strategy min-gpu --strategy min-workloads --strategy min-users \
  --candidate-scope all --alternatives 3 \
  --violation-code utilization.low_gpu_activity --format json
python3 skill/clusterx-manage-jobs/scripts/monitor_cli.py watch \
  --view alerts --count 10 --format jsonl
```

监控服务不可用时 CLI 明确失败，不直接查询集群。可用 `--endpoint` 或
`CLUSTERX_MONITOR_URL` 修改地址。查询成功即退出 `0`；参数错误为 `2`，服务
不可用为 `3`，`--fail-on` 命中为 `4`，用户中断为 `130`。

为了让管理员修改后的训练 CPU 规则同时约束任务提交，在运行 Skill 包装器的
环境中设置同一个本地文件：

```bash
export CLUSTERX_RESOURCE_POLICY="$PWD/config/resource-policy.local.json"
```

也可以在每次调用 `clusterx_exec.py` 时显式传入 `--resource-policy`。Monitor
仍不是任务 CRUD 的运行时依赖；包装器直接读取该本地文件。

## 策略摘要

- 0-GPU 开发机：每节点最多 8 CPU、140 GiB，不限制运行时长。
- 1-GPU 开发机：每节点最多 14 CPU、240 GiB，最长 72 小时；更多 GPU 不允许。
- 训练任务每 GPU 最多 14 CPU、240 GiB；0-GPU 任务最多一个等价 slice。
- 标准调度画像默认每 GPU 需要 14 CPU、240 GiB；它只用于未显式填写 CPU/内存
  的方案查询以及节点 effective/blocked 展示，不是额外的提交下限。显式方案需求
  会覆盖画像，训练资源比例仍只表示提交和策略上限。
- 至少 1 个训练任务排队满 10 分钟后激活 quota pressure。
- 分组 CPU/内存 quota 从 GPU quota 按 14/240 派生。
- 无 pressure 时超额为 burst，有 pressure 时为 violation。
- 私有配置中的显式 quota 保持不缩放；默认组获得绑定 GPU 总容量的剩余部分。
- 当前仍在运行、使用 GPU 且已运行至少 60 分钟的 `trainingJob`/`aid`，过去
  24 小时 GPU compute 样本加权平均与显存容量/时间加权平均同时 `<=20%` 时，
  产生 `utilization.low_gpu_activity` 违规；0-GPU、预热中或缺指标均不判违规。
- 低利用率违规传播到 workload 与用户，不改变分组自身 quota/burst 状态。

本地资源策略与私有分组文件都会热加载；非法新配置不会替换 last-known-good
组合。缺失配置通过受保护的管理员初始化流程补全，而不是开放匿名写入。
Pod 汇总超过节点 allocated 的归属异常节点仍出现在监控中，但其节点以及任何
落在该节点上的 workload 都会从调度模拟排除；未知归属资源也永不声明为可释放。

## Clusterx 配置与 smoke 项目

从 `skill/clusterx-manage-jobs/assets/clusterx.example.yaml` 创建权限为 `600`
的全局或项目配置。项目 `.clusterx/clusterx.yaml` 完整覆盖全局配置，不做字段
合并；真实密钥、挂载 ID、内部 endpoint 和签名 URL 不得进入 Git 或聊天。

`smoke-projects` 用于验证安装后的 Skill，但不进入发布包。可请求 Skill 提交
GPU 计算、持续日志或组合存储 smoke test；任务清单描述资源需求，Skill 根据
动态帮助构造实际 Clusterx 命令。Monitor 不参与这些任务的创建、查询或停止。

## 开发与验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
cd web && npm test && npm run build
python3 scripts/package_skill.py --output-dir dist
```

Skill 发布包仍只包含 `skill/clusterx-manage-jobs`，不包含 Web、服务端核心、
Clusterx 凭据、私有分组或开发维护工具。打包器会拒绝 `lark-cli`、飞书认证、
来源清单和维护脚本进入运行时 Skill。

## 飞书文档维护与 Clusterx 更新

只有维护静态参考和 Clusterx 安装来源的仓库开发者需要 `lark-cli` 与权威飞书
文档权限；安装后的 Skill 不访问飞书。维护脚本默认移除代理环境变量，只有
确有需要时才使用 `--keep-proxy`。

从权威文档读取当前临时 wheel、下载到临时目录、校验包名并安装到指定环境：

```bash
python3 scripts/maintenance/install_clusterx.py \
  --python /path/to/venv/bin/python
```

检查飞书文档更新并生成脱敏候选、diff 和报告：

```bash
python3 scripts/maintenance/check_updates.py \
  --source clusterx-main \
  --staging-dir /tmp/clusterx-skill-update
```

审阅 `report.json`、`candidate.md` 和 `diff.patch` 后，人工更新 Skill reference
以及 `scripts/maintenance/sources.json` 的 revision、脱敏指纹和审核时间，再运行
完整测试与打包校验。脚本不保存签名 URL，临时 wheel 会随临时目录删除。

不要提交访问密钥、Token、Cookie、私钥、签名 URL 或真实挂载凭据。
