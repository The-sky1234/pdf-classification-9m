# Agent职责/目录/协作矩阵（V3冻结）

## 职责矩阵
| Agent | 主责 | 不做 |
|---|---|---|
| Planner | 契约唯一维护+任务拆解 | 业务代码 |
| Parser | 检测+直提+按需OCR | 判决 |
| Classification | 三门+4/27+二审 | 拼路径/落盘 |
| Backend | API+台账+复核+归档持久化 | 算法 |
| Scheduler | Registry/Dedup/队列/Archive复制 | 信队列路径 |
| DevOps | 单机容器/GPU/存储监控 | 业务 |
| QA | 27+实体+恢复验收 | 实现 |

## 目录所有权
| 目录 | 主人 |
|---|---|
| contracts/, docs/ | Planner |
| parser/ | Parser |
| classifier/ | Classification |
| backend/ | Backend |
| scheduler/ | Scheduler |
| deploy/ | DevOps |
| tests/, reports/ | QA（各Agent可写自有tests子目录） |
| .cursor/rules/ | Planner统一维护 |

## 协作与变更
他Agent只读contracts；变更提Issue→Planner改→QA验→各Agent跟进；禁并发同文件；跨目录由Planner协调。
## 变更摘要/遗留
改7个md到V3（专利/低价值/27/归档/按需）；rules 4份沿用；备份因Shell中文路径未に戻、原文件已Read留痕；缺失docs/requirements|design|references已在OPEN_ISSUES声明；未动业务代码库表部署。
