# Clusterx Manage Jobs Skill

用于安全管理 PT/SSP 集群 Clusterx 训练任务的 Codex Skill 项目。
仅限具备内部 Clusterx 访问资格的公司用户使用。

当前 Skill 发布版本为 `0.2.0`。已验证 Clusterx CLI `2026.8.11`；
其他版本可使用，但应以安装后的动态帮助为准并视为尚未验证。

## 项目结构

```text
.
├── README.md
├── tests/
└── skill/
    └── clusterx-manage-jobs/   # 可独立安装的 Skill
        ├── SKILL.md
        ├── agents/openai.yaml
        ├── references/
        └── scripts/
```

安装或分发时只复制 `skill/clusterx-manage-jobs`；仓库根目录保存项目说明、
测试、维护工具、文档来源和安装后的 smoke 示例，不属于 Skill 安装包。

## 功能说明

- 检查 `clusterx` 命令、配置文件权限、必填配置项和共享 `tmpdir`。
- 为训练任务生成资源、镜像、挂载和脱敏命令预览；用户已明确要求提交时直接执行。
- 查询任务、节点、日志和统计信息；用户已明确指定准确目标时直接停止任务。
- 汇总队列节点上的训练任务、开发机及其他工作负载，诊断 GPU 碎片并生成只读的完整节点释放候选。
- 对配置、命令、复杂挂载 JSON、HTTP 认证头和签名 URL 做脱敏。
- 提交前拒绝 Clusterx 无法安全保留参数边界的 `bash/sh -c/-lc` 写法，
  要求直接调用 runner 脚本。
- 支持 `.dev-env` 全局配置与项目 `.clusterx/clusterx.yaml` 完整覆盖。

Skill 将用户的明确请求视为对应操作的授权，不做重复确认；目标含糊、参数缺失
或执行过程新增特权模式等风险升级时才询问。测试不会连接真实集群。

## 安装指南

推荐把 Skill 目录复制或软链接到 Codex 的 skills 目录：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -a skill/clusterx-manage-jobs \
  "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs"
```

复制完成后安装终端美化依赖：

```bash
python3 -m pip install -r \
  "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs/requirements.txt"
```

`rich` 缺失时队列分析 CLI 会自动回退到无颜色纯文本，不影响只读分析。

开发期间也可以把安装路径软链接到源码目录；仓库移动或目录重构后，应同步
检查软链接目标下是否仍能直接看到 `SKILL.md`。

Clusterx CLI 只能在 Linux 集群开发机上使用，要求 Python 3.10 或更新版本。
2026.8.11 是本项目已验证版本；其他版本应以安装后动态帮助为准。

Clusterx CLI 是独立的运行时前置条件，不随 Skill 分发。请通过公司内部当前
Clusterx 分发渠道安装到隔离的 venv/Conda 环境，再安装和使用本 Skill。
Skill 本身不会访问飞书或代为安装 Clusterx。

安装后验证：

```bash
python3 --version
clusterx --version
clusterx --help
```

安装后可从任意目录执行队列碎片分析；`--cwd` 可省略，此时从当前目录向上
查找项目配置，并继续按全局配置优先级回退：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs/scripts/queue_plan.py" \
  --nodes 2
```

需要解析另一个项目的配置时显式传入目录：

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs/scripts/queue_plan.py" \
  --cwd /path/to/project --nodes 2 --strategy all \
  --alternatives 3 --search-seconds 10
```

默认只分析碎片节点；如需让已占满 GPU 的节点任务也参与只读候选优化，增加
`--candidate-scope all`。也可使用 `--candidate-scope full` 仅比较完整节点。
每个策略默认返回最多 3 个方案；可用 `--alternatives 1..10` 调整。求解阶段
默认使用 `--search-seconds 10`，根据当前机器实测状态吞吐在精确搜索和启发式
搜索之间切换。

汇总表默认显示 `--minutes` 窗口内每个 workload 的 GPU 算力、显存平均利用率
及单卡范围。需要定位具体卡时增加 `--show-gpu-details`；JSON 始终保留逐卡
数据。利用率仅供观察，不参与释放方案排序。

队列分析使用与网页“查看负载”相同的节点 Pod 数据，可显示并归因开发机
（`aid`）、训练任务（`trainingJob`）及其他工作负载。`--strategy min-workloads`
用于最小化需要协调的工作负载数量；旧参数 `--strategy min-jobs` 保留为兼容别名。

首次配置后，保护配置文件权限：

```bash
mkdir -p ~/.config
cp skill/clusterx-manage-jobs/assets/clusterx.example.yaml \
  ~/.config/clusterx.yaml
chmod 600 ~/.config/clusterx.yaml
```

在本机编辑配置占位符，不要把真实密钥粘贴到聊天中。然后执行：

```bash
python3 skill/clusterx-manage-jobs/scripts/preflight.py \
  --cwd /path/to/project \
  --tmpdir /path/shared/by/dev-machine-and-job
```

项目需要独立配置时，创建完整的 `.clusterx/clusterx.yaml` 并设置为 `600`。
Skill 按“显式配置、环境变量、项目配置、显式 `DEV_ENV` 全局配置、原生默认
路径”的顺序选择。

不要把访问密钥、Token、Cookie、私钥、签名 URL 或真实挂载凭据提交到 Git。

## 开发与测试

运行时 Skill 不依赖 `lark-cli`。仓库测试仅使用临时目录和模拟的
`clusterx`/`lark-cli`：

```bash
python3 -m unittest discover -s tests -v
python3 \
  /path/to/skill-creator/scripts/quick_validate.py \
  skill/clusterx-manage-jobs
```

官方 `quick_validate.py` 需要 PyYAML；若要在 base 环境直接运行校验器，
需先在可访问的内部软件源安装 `PyYAML`。运行时核心逻辑使用 Python 标准库；
`requirements.txt` 中的 `rich` 用于彩色表格，并有纯文本降级路径。

脱敏器从标准输入读取，避免凭据出现在进程参数中：

```bash
printf '%s\n' 'access_token=example' |
  python3 skill/clusterx-manage-jobs/scripts/redact.py
```

统一包装器会自动脱敏 Clusterx 的 stdout/stderr；不要绕过包装器直接展示
`clusterx run` 的调试输出，因为当前 CLI 会打印包含挂载配置的 job spec。

## 安装后快速体验

`smoke-projects` 不进入 Skill 安装包；它用于从本仓库快速验证安装好的 Skill。
可直接对 Codex 提出：

```text
使用 $clusterx-manage-jobs，分析当前队列申请 2 个完整 8 卡节点的资源整理方案，
展示最少 GPU、最少工作负载和最少用户的候选，每种策略最多给 3 个方案，不要停止工作负载。
```

```text
使用 $clusterx-manage-jobs，汇总当前队列每个碎片节点上的用户、工作负载类型、GPU
占用和最近 10 分钟负载。
```

```text
使用 $clusterx-manage-jobs，检查 2 个节点、每节点 8 GPU、64 CPU、800 GiB
内存是否可调度；如果不能，给出只读整理候选。
```

```text
使用 $clusterx-manage-jobs，根据 smoke-projects/gpu-matmul/project.json
提交 GPU smoke test，并持续查询到终态。
```

```text
使用 $clusterx-manage-jobs，根据 smoke-projects/storage-access/project.json
提交一个同时验证当前 Clusterx 配置中全部文件存储和对象存储挂载的任务。
```

任务清单只描述资源需求，Skill 会根据已安装 CLI 的动态帮助生成实际命令。

## 发布 Skill

维护者可生成只包含 Skill 目录的归档及 SHA-256：

```bash
python3 scripts/package_skill.py --output-dir dist
cd dist
sha256sum -c clusterx-manage-jobs.tar.gz.sha256
```

打包器会拒绝任何包含 `lark-cli`、飞书认证流程、来源清单或维护脚本的 Skill。

## 开发者维护

只有维护 Skill 静态参考的开发者需要安装 `lark-cli` 并拥有权威飞书文档权限。
维护脚本默认移除代理环境变量后直接访问飞书；只有确实需要代理时才传
`--keep-proxy`。

从权威飞书文档读取当前签名 wheel、临时下载、校验并安装到指定隔离环境：

```bash
python3 scripts/maintenance/install_clusterx.py \
  --python /path/to/venv/bin/python
```

脚本不会保存签名 URL，下载的 wheel 在安装结束后随临时目录删除。

检查文档更新时运行：

```bash
python3 scripts/maintenance/check_updates.py \
  --source clusterx-main \
  --staging-dir /tmp/clusterx-skill-update
```

审阅生成的 `report.json`、`candidate.md` 和 `diff.patch` 后，更新
`skill/clusterx-manage-jobs/references/clusterx-cli.md` 以及
`scripts/maintenance/sources.json` 中的 revision、脱敏源 SHA-256 和审核时间。
`changed` 根据脱敏后的源文档指纹判断；`reference_differs` 仅表示飞书原文与
人工整理的 reference 文本不同。最后运行完整测试与打包校验。

不要提交原始飞书文档、签名 URL 或 wheel。本仓库按 [LICENSE](LICENSE)
所述仅限公司内部授权使用。
