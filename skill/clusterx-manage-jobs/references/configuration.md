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
python3 scripts/clusterx_exec.py --cwd <project-dir> -- get-job <job-id> --workers
python3 scripts/clusterx_exec.py --cwd <project-dir> -- log <job-id> --worker <worker-name>
python3 scripts/clusterx_exec.py --cwd <project-dir> -- log <job-id> --hours 6
```

包装器只报告配置来源和路径，不输出配置值。

训练任务提交还读取不含用户或分组信息的
`assets/resource-policy.json`。有 GPU 时 CPU 上限为
`GPU × cpu_per_gpu`，0 GPU 时使用 `zero_gpu_max_cpu_per_node`。可在包装器的
`--` 之前传 `--resource-policy <path>`，也可设置
`CLUSTERX_RESOURCE_POLICY`；显式参数优先于环境变量，环境变量优先于 Skill
内置策略。该校验不依赖 monitor 服务。

队列监控和调度模拟只访问本机 `clusterx-monitor` 服务的缓存，不读取上述
Clusterx 配置，也不会在服务不可用时回退到实时采集：

```bash
python3 scripts/monitor_cli.py status --format json
python3 scripts/monitor_cli.py overview
python3 scripts/monitor_cli.py groups --violations-only
python3 scripts/monitor_cli.py plan --nodes 2 --gpus-per-node 8 \
  --strategy min-gpu --strategy min-workloads \
  --candidate-scope all --alternatives 3
python3 scripts/monitor_cli.py watch --view alerts --count 10 --format jsonl
```

服务地址默认是 `http://127.0.0.1:8765`。可以在子命令之前传
`--endpoint`，或设置 `CLUSTERX_MONITOR_URL`。安装 Skill 后从任意目录调用时，
使用 `${CODEX_HOME:-$HOME/.codex}/skills/clusterx-manage-jobs/scripts/monitor_cli.py`
的完整路径。

Monitor 服务端另外要求一份权限为 `600` 的本地私有分组文件。仓库开发时从
`config/groups.example.yaml` 复制为被 Git 忽略的
`config/groups.local.yaml`，只在本机填写真实组名、quota 和拼音用户名。
服务通过 `--policy-config` 加载公共策略，通过 `--group-config` 加载私有分组；
首次缺失或校验失败时服务进入受认证的 `setup-required`，管理员可在 Web 中查看
损坏文件的原始 JSON/YAML 并修复；两份文件均有效后开始采集，无需重启。运行中
的错误继续使用完整 last-known-good 组合。Monitor 对 Clusterx 只读，但管理员
界面会以 revision 校验、备份和原子替换方式写入这两份本地配置。

公共策略中的 `planning.default_cpu_per_gpu` 与
`planning.default_memory_gib_per_gpu` 是标准调度画像。方案未显式给出 CPU/内存
时由快照内画像推导；节点的 `effective_free_gpu`、`stranded_gpu` 和
`cpu-memory-blocked` 也相对于该画像，不表示更小的显式任务一定无法调度。
