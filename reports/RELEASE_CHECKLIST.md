# 生产发布检查表（2026-10-08修复轮更新；代码门禁已过，上机验证仍待执行）

## 代码与契约
- [x] 4个一级/25个二级齐全，父子约束 enforced
- [x] IRRELEVANT / MANUAL_REVIEW / FAILED 终态齐全
- [x] TBD边界保守策略生效
- [x] ISSUE-03 evidence_mode四值统一（classification）
- [x] ISSUE-04 非CLASSIFIED禁带类别（backend）
- [x] ISSUE-05/10 代码质量修复并复检
- [ ] backend `/metrics` 端点（ISSUE-06剩余跟进，不阻塞功能上线）

## 数据链路（阻塞→已解除）
- [x] ISSUE-01 跨Worker交接实现 + e2e复检通过（50 passed含真实交接用例）
- [x] ISSUE-02 compose鉴权变量补齐 + 回写401/201复检
- [x] ISSUE-09 GPU租约真实生效 + 跨进程复检

## 阈值与验收（门禁生效，数值待签署）
- [ ] 首批样本人工标注完成（含无关/边界/无标题/纯图）
- [ ] 生产阈值签署并以THRESHOLDS_CALIBRATED=1启用（未签署前自动归档被强制阻断）
- [ ] 精确率/覆盖率/复核成本三方确认（balanced路线）

## 真实Linux GPU上机Benchmark清单（逐项实测，禁止理论值充数）
- [ ] `bash deploy/gpu_check.sh` 全绿
- [ ] 4镜像构建成功并启动，`./ops.sh health`三项OK
- [ ] 1000份混合样本端到端跑通
- [ ] 分阶段耗时统计并代入`estimate_full_run`，`meets_90d`为真才排工期
- [ ] 断电/kill演练续跑无丢失无重复
- [ ] 监控告警有数

## 发布纪律
- [x] Mock测试≠GPU测试已在报告书面区分（真实CPU 9、Mock 41、GPU 0）
- [ ] 未经实测不承诺“三个月完成900万”（保持）
- [ ] .env与密钥不在仓库留存（上机时执行）
