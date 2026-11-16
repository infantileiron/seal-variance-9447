# 工艺归并验证记录（gb-3105）

## 环境

本批次在无 Docker 的容器内验证，等效组件均以用户态运行：

- PostgreSQL 16.4（源码编译，`/workspace/.toolchain/pg`，端口 17704）
- Redis 7.2.5（源码编译，端口 6379）
- backend：uvicorn 127.0.0.1:19204；worker：`python -m src.worker`（`MERGE_BATCH_SIZE=50`）
- frontend：静态服务 127.0.0.1:18204（`/.toolchain/serve-frontend.js`，`/api` 代理至后端）
- UI：playwright-core + Chromium headless（系统库以 `.deb` 解包至 `.toolchain/libs` 提供）

启停脚本：`/workspace/.toolchain/stack.sh {start|stop|restart|start-worker|stop-worker}`。

## API 验证：`test_merge_api.py`（41 项）

运行：`bash run_api_tests.sh`（重置库、seed、执行；约 6 分钟，创建约 250 条样本）。
| 场景 | 覆盖需求 |
|---|---|
| S1 预览参数差异/样本影响、只读无副作用 | 预览参数差异及样本影响 |
| S2 别名即时生效、收样重定向、分批迁完核对一致、别名冻结 | 旧编号作为别名；新收样统一归入目标；迁完且数量核对一致才结束 |
| S3 归并前后评议快照与结果 checksum 不变 | 旧评议保留原依据 |
| S4 别名与目标同选去重为一组；同组拒绝 | 源与目标同时入选按一个工艺分析 |
| S5 并发反向归并恰好一个 201；完成后反向仍 409 | 反向归并争抢；循环归并拒绝 |
| S6 链式归并落到最终工艺；C→A 循环拒绝；链上同选 422 | 归并链落到最终工艺 |
| S7 kill -9 worker 于迁移半途，重启后续传完成，编号集合与总数不变 | 中断后从未完成处恢复；同次归并重试不重复移动样本 |
| S8 迁移中并发收样 12 条+修订目标参数+创建评议，零丢失、快照不混组 | 并发收样、修订参数和创建评议时，不丢样、不混用归并前后分组 |
| S9 迁移中修订源上样本：重定向入目标且计入迁出数 | 归并期间照常收样（含修订路径） |
| S10 人为制造数量不一致 → failed → 重试后完成且不重复移动 | 迁完且数量核对一致才结束；失败重试 |
| S11 viewer 只读（可看/可预览/不能发起） | 权限 |
| S12 同一对并发发起恰好一个成功 | 并发争抢 |
| S13 目标为别名时归一到最终工艺 | 归并链落到最终工艺 |
| S14 归并动作写入操作审计 | 留痕 |

最近一轮结果：**41/41 通过**（日志 `/tmp/merge-test-run3.log`）。

## UI 验证：`test_merge_ui.js`（13 项）

运行：先 `SEED_DEMO=true /workspace/.toolchain/stack.sh restart`（worker 需运行），
`cd /workspace/.toolchain && LD_LIBRARY_PATH=... node /workspace/test-artifacts/gb-3105/test_merge_ui.js`。

覆盖：登录后方案库出现归并区 → 预览展示参数差异与样本影响（截图 `ui-merge-preview.png`）
→ 确认发起并提示别名生效（`ui-merge-migrating.png`）→ 页面轮询追踪到“归并完成 6/6、剩余 0”
（`ui-merge-completed.png`）→ 旧编号别名标记 → 收样下拉别名选项与自动归入（`ui-samples-alias.png`）
→ 分析页仅列正主、目标标注“含别名 …合并分析”（`ui-analysis-alias.png`）→ 只读账号无发起表单 → 全程无脚本错误。

## 结论

归并预览/发起/追踪、别名重定向、分批迁移核对、中断恢复、链式与循环防护、并发不丢样不混组，
在真实 PostgreSQL+Redis+worker 链路上全部通过；旧评议快照与结果不受归并影响。
