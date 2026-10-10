# Archive契约 v3.0（冻结）
- decide_target只查taxonomy-v3映射，禁模型拼路径；realpath校验SOURCE_ROOT/ARCHIVE_ROOT防穿越/symlink。
- 复制：只读源→安全名→同盘tmp→流拷→大小+SHA→源未变→os.replace→PG事务COMPLETED。
- 幂等：final存在且验过即COMPLETED；仅tmp则删重来；PG未交则重入队；O_EXCL防双写。
- 重归档：version+1，旧版丢弃，新成后旧标SUPERSEDED，禁删源。
- 迁移002：archive_tasks(file_id,version,target,sha,status)+archive_audit+results加target_path/archive_status/index。
