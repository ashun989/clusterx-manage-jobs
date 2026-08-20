# Clusterx 在 SSP 上无法查询任务日志（已于 2026.8.19 解决）

## 解决状态

Clusterx `2026.8.19` 已修复实时日志查询，并新增历史日志搜索。新版不再通过
`clusterx log <JOB_ID>` 隐式发现全部 Pod：先用 `get-job --workers` 获取 Worker，
再指定一个 Worker 查询实时日志；历史日志则通过时间或内容条件触发。

```bash
clusterx get-job <JOB_ID> --workers
clusterx log <JOB_ID> --worker <WORKER_NAME> --lines 200
clusterx log <JOB_ID> --hours 6
```

历史日志还支持 `--start/--end`、`--msg`、可选 `--worker`、每页最多 1000 条和
自动翻页上限；相同查询条件最多返回 10,000 条。以下内容保留为旧版本问题记录。

## 环境

- Clusterx 版本：`2026.7.1`
- 集群类型：SSP
- 测试日期：2026-07-27
- 操作系统与具体集群、队列、镜像、用户及存储信息：已脱敏

## 问题描述

Clusterx 可以正常提交 SSP 任务，也可以查询任务状态。测试任务能够从
`Queuing` 进入 `Running`，最终变为 `Succeeded`。

但是，在任务处于 `Running` 和 `Succeeded` 状态时执行
`clusterx log`，均无法获得标准输出或标准错误日志。

## 最小复现任务

测试程序持续运行约 60 秒，每 5 秒输出并立即 flush 一条 JSON 进度记录，
共输出 12 次。程序结束时还会向共享文件存储写入结构化结果文件。

示例逻辑：

```python
import json
import time

for step in range(1, 13):
    print(json.dumps({"event": "progress", "step": step}), flush=True)
    time.sleep(5)
```

任务资源：

- 节点数：1
- GPU：0
- CPU：1
- 内存：4 GiB
- 共享内存：1 GiB

提交命令结构：

```bash
clusterx run \
  -J <JOB_ID> \
  -N 1 \
  --gpus-per-task 0 \
  --cpus-per-task 1 \
  --memory-per-task 4 \
  --shm-size-gib 1 \
  --no-env \
  python <SCRIPT_PATH>
```

查询日志：

```bash
clusterx log <JOB_ID> --lines 200
```

## 实际结果

任务处于 `Running` 时，`clusterx log` 请求以下接口并得到 HTTP 404：

```text
trainingJobs/<JOB_ID>/pods?page_size=100
```

任务变为 `Succeeded` 后再次查询，仍返回相同的 HTTP 404：

```text
[API Error] 404: {"code":5,"message":"Not Found"}
Failed to get logs for job <JOB_ID>
Error retrieving logs: 404 Client Error
```

同时观察到：

- `get-job` 能正常返回任务状态。
- 任务详情中的 `nodes` 和 `nodes_ip` 均为空。
- 当前 `get-node` 对 SSP 的支持不完整。
- 共享文件存储中的最终结果存在，内容显示 `ok: true`。
- 程序确实执行了全部 12 个步骤，总运行时间约 60 秒。

因此可以排除任务未启动、程序未输出、Python 输出缓冲以及共享存储不可用等原因。

## 预期结果

- 任务处于 `Running` 时，`clusterx log` 能读取已经 flush 的 stdout/stderr。
- 任务完成后，`clusterx log` 仍能读取保留的任务日志。
- 若日志查询依赖 pod 信息，任务详情或 pods 接口应返回对应的 pod。

## 初步判断

任务提交、调度、执行、状态查询和存储挂载均正常。问题可能位于以下环节之一：

1. SSP 没有为 TrainingJob 暴露或持久化 pod 元数据。
2. `trainingJobs/<JOB_ID>/pods` 接口的路径、权限或资源映射异常。
3. Clusterx 2026.7.1 使用了不适用于当前 SSP API 的 pods 查询方式。
4. 任务完成后 pod 被快速回收，但运行期间返回 404 表明这不是唯一原因。

## 希望协助确认

1. 当前 SSP 是否支持通过 Clusterx 查询运行中及历史任务日志？
2. `trainingJobs/<JOB_ID>/pods` 返回 404 是否符合预期？
3. SSP 是否有其他日志接口，或是否需要额外配置、权限或命令参数？
4. 为什么任务状态可查询，但 `nodes`、`nodes_ip` 和 pods 信息为空？
5. 该问题是否已在 Clusterx 的更新版本中修复？

## 补充问题

SSP 服务端要求任务名称长度不超过 32 个字符。超过限制时，创建请求返回：

```text
invalid TrainingJob.Name: value length must be between 1 and 32 runes, inclusive
```

建议 Clusterx 在发起请求前进行客户端校验并给出更直接的提示。
