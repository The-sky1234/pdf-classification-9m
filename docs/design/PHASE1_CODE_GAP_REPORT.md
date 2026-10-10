# PHASE1_CODE_GAP_REPORT（差异审查）
可复用：backend 8路由/store门禁/2表；parser三路+evidence；classifier漏斗+二审+UNRESOLVED；scheduler队列重试；compose编排。
需改：taxonomy 25→27（planner/taxonomy.yaml，风险高）；gate加PatentGate/LowValueGate前置；review支持UNRESOLVED；results加target/archive字段；review队列含UNRESOLVED。
新增：Registry/Dedup/Features/ArchiveWorker/archive API；迁移002。
冲突：旧IRRELEVANT≈LV04需映射；旧25名与Excel差离退休/纪检等。
未测：真GPU（OCR/MinerU/BGE）、真机compose、阈值校准、Archive故障注入。
