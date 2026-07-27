# Clusterx 配置分层

## 配置位置与优先级

Clusterx 本身通过 `CLUSTERX_CFG_PATH` 读取指定配置。使用本 Skill 时，按以下
顺序选择一份完整配置：

1. 包装器的 `--config`；
2. 环境变量 `CLUSTERX_CFG_PATH`；
3. 从当前工作目录向上找到的最近 `.clusterx/clusterx.yaml`；
4. `${DEV_ENV:-/data/zengquansheng/.dev-env}/clusterx/clusterx.yaml`；
5. 原生默认路径 `~/.config/clusterx.yaml`。

项目配置完整替换全局配置，不做字段合并，也不生成包含密钥的临时文件。

## 全局配置

将跨项目共用的持久化配置保存到：

```text
/data/zengquansheng/.dev-env/clusterx/clusterx.yaml
```

现有开发机 bootstrap 会将其链接到 `~/.config/clusterx.yaml`。文件及链接
最终目标必须仅允许所有者访问：

```bash
chmod 600 /data/zengquansheng/.dev-env/clusterx/clusterx.yaml
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
python scripts/preflight.py --cwd <project-dir> --tmpdir <shared-tmpdir>
```

通过统一包装器调用 Clusterx：

```bash
python scripts/clusterx_exec.py --cwd <project-dir> -- list
python scripts/clusterx_exec.py --cwd <project-dir> -- run <arguments>
python scripts/clusterx_exec.py --cwd <project-dir> -- log <job-id>
```

包装器只报告配置来源和路径，不输出配置值。
