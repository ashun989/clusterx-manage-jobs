# Clusterx 配置分层

## 配置位置与优先级

Clusterx 本身通过 `CLUSTERX_CFG_PATH` 读取指定配置。使用本 Skill 时，按以下
顺序选择一份完整配置：

1. 包装器的 `--config`；
2. 环境变量 `CLUSTERX_CFG_PATH`；
3. 从当前工作目录向上找到的最近 `.clusterx/clusterx.yaml`；
4. 显式设置 `DEV_ENV` 时的 `${DEV_ENV}/clusterx/clusterx.yaml`；
5. 原生默认路径 `~/.config/clusterx.yaml`。

项目配置完整替换全局配置，不做字段合并，也不生成包含密钥的临时文件。
可从 `assets/clusterx.example.yaml` 复制一份无密钥 SSP 模板，在本机填写
占位符后设置为 `600`。不要把填写后的配置加入 Git。

模板中的 `mount` 演示一份完整的多挂载配置：两个 `PV_AFS` 文件存储和两个
`PV_AOSS` 对象存储。按实际需求删除多余条目；文件存储填写 `id` 和
`mount_path`，对象存储填写 `name`、`endpoint`、`mount_path` 以及受保护的
`metadata.items` 凭据。不要把真实挂载 ID、内部 endpoint、路径或凭据写回模板。

## 全局配置

默认将跨项目共用的持久化配置保存到：

```text
~/.config/clusterx.yaml
```

如团队开发环境显式设置了 `DEV_ENV`，也可保存到
`${DEV_ENV}/clusterx/clusterx.yaml`。文件及链接最终目标必须仅允许所有者访问：

```bash
chmod 600 ~/.config/clusterx.yaml
```

## 项目配置

项目需要不同集群、队列、镜像、挂载或凭据时，在项目根目录创建：

```text
.clusterx/clusterx.yaml
```

它必须包含一份完整有效的 Clusterx 配置，并设置为 `600`。将 `.clusterx/`
加入项目 `.gitignore`，不要提交真实配置。

## Skill 命令入口

先检查配置：

```bash
python3 scripts/preflight.py --cwd <project-dir> --tmpdir <shared-tmpdir>
```

通过统一包装器调用 Clusterx：

```bash
python3 scripts/clusterx_exec.py --cwd <project-dir> -- list
python3 scripts/clusterx_exec.py --cwd <project-dir> -- run <arguments>
python3 scripts/clusterx_exec.py --cwd <project-dir> -- log <job-id>
```

包装器只报告配置来源和路径，不输出配置值。

通过只读队列分析器检查完整节点调度和 GPU 碎片：

```bash
python3 scripts/queue_plan.py --nodes 2
python3 scripts/queue_plan.py --cwd <project-dir> --nodes 2 --strategy min-gpu
python3 scripts/queue_plan.py --nodes 2 --strategy min-workloads
python3 scripts/queue_plan.py --nodes 2 --candidate-scope all
python3 scripts/queue_plan.py --nodes 2 --alternatives 3 --search-seconds 10
python3 scripts/queue_plan.py --nodes 2 --refresh-seconds 30
```

`--cwd` 可省略；省略时以当前工作目录为配置发现起点。安装 Skill 后从任意
目录调用时，使用
`${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs/scripts/queue_plan.py`
的完整路径。
