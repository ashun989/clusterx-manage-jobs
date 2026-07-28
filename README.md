# Clusterx Manage Jobs Skill

用于安全管理 PT/SSP 集群 Clusterx 训练任务的 Codex Skill 项目。

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
测试和开发配置，不属于 Skill 安装包。

## 功能说明

- 检查 `clusterx` 命令、配置文件权限、必填配置项和共享 `tmpdir`。
- 为训练任务生成资源、镜像、挂载和脱敏命令预览；用户已明确要求提交时直接执行。
- 查询任务、节点、日志和统计信息；用户已明确指定准确目标时直接停止任务。
- 对配置、命令、复杂挂载 JSON、HTTP 认证头和签名 URL 做脱敏。
- 支持 `.dev-env` 全局配置与项目 `.clusterx/clusterx.yaml` 完整覆盖。
- 从登记的权威飞书文档生成已脱敏候选版本和差异；明确要求更新时审阅后直接应用。

Skill 将用户的明确请求视为对应操作的授权，不做重复确认；目标含糊、参数缺失
或执行过程新增特权模式等风险升级时才询问。测试不会连接真实集群。

## 安装指南

推荐把 Skill 目录复制或软链接到 Codex 的 skills 目录：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -a skill/clusterx-manage-jobs \
  "${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs"
```

开发期间也可以把安装路径软链接到源码目录；仓库移动或目录重构后，应同步
检查软链接目标下是否仍能直接看到 `SKILL.md`。

Clusterx CLI 只能在集群开发机上使用。请从可信的内部软件源获取当前版本，
不要复用文档中的过期签名下载链接。建议在隔离的 conda 环境安装 CLI；若明确
需要在 base 环境验证，请先确认：

```bash
conda activate base
python --version
clusterx --version
clusterx --help
```

首次配置后，保护配置文件权限：

```bash
chmod 600 /data/zengquansheng/.dev-env/clusterx/clusterx.yaml
python skill/clusterx-manage-jobs/scripts/preflight.py \
  --cwd /path/to/project \
  --tmpdir /path/shared/by/dev-machine-and-job
```

项目需要独立配置时，创建完整的 `.clusterx/clusterx.yaml` 并设置为 `600`。
Skill 按“显式配置、环境变量、项目配置、全局配置、原生默认路径”的顺序选择。

不要把访问密钥、Token、Cookie、私钥、签名 URL 或真实挂载凭据提交到 Git。

## 开发与测试

测试仅使用临时目录和模拟的 `clusterx`/`lark-cli`：

```bash
conda run -n base python -m unittest discover -s tests -v
python \
  /path/to/skill-creator/scripts/quick_validate.py \
  skill/clusterx-manage-jobs
```

官方 `quick_validate.py` 需要 PyYAML；若要在 base 环境直接运行校验器，
需先在可访问的内部软件源安装 `PyYAML`。本项目功能脚本本身仅依赖 Python
标准库。

脱敏器从标准输入读取，避免凭据出现在进程参数中：

```bash
printf '%s\n' 'access_token=example' |
  python skill/clusterx-manage-jobs/scripts/redact.py
```
