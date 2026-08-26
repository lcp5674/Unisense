# Unisense 备份与恢复 Runbook（RTO ≤ 4h / RPO ≤ 15min）

> 对齐等保 2.0 §8.1.6.1（备份与恢复）与 TD §18 韧性要求。
> 备份对象：MySQL（业务主库，权威数据）、Neo4j（血缘图，可重建）、
> Elasticsearch（检索索引，可重建）、审计归档（MinIO）。

---

## 一、备份策略总览

| 数据 | 方式 | 频率 | 保留 | 介质 |
|------|------|------|------|------|
| **MySQL** | 每日全量 `mysqldump --single-transaction` + **binlog 增量**（ROW 格式，RPO≤15min） | 全量每 24h（启动即跑一次 + 循环，见 §二.1 说明）；增量每 5 分钟 | 7 天 | `unisense_backups` 卷 / 外部存储 |
| Neo4j | `neo4j-admin database dump` | 每日 | 7 天 | 节点本地 + 转存 |
| Elasticsearch | snapshot API → 仓库 | 每日 | 7 天 | ES snapshot 仓库 |
| 审计归档 | MinIO 对象（应用层已归档） | 持续 | 180 天 | MinIO 卷 |

> **RPO 说明（2026-08-13 起）**：MySQL 采用「每日全量 + binlog 增量」双层备份。
> 恢复链 = 最新全量备份 + 该全量时刻之后的所有 binlog 回放，极端情况下最多丢失
> **15 分钟**数据（binlog 归档周期 5 分钟 + 处理余量）。全量仍保留 7 天。

## 二、备份操作

### 1. MySQL 全量（自动，每 24h 循环）

由 `docker-compose.yml` 的 `backup` 服务执行。注意：**全量备份为「容器启动即跑一次 + 每 24 小时循环」，并非固定每日 02:00**——依赖容器持续运行，重启容器会提前触发一轮全量（幂等，覆盖旧文件）。

- 支持**多库备份**：默认备份 `unisense` 主库；可通过 `UNISENSE_BACKUP_DATABASES="unisense e2e_biz"` 同时备份降级业务库 `e2e_biz`。
- **失败告警**：任一库备份失败时 `backup.sh` 退出非 0，backup 服务捕获后打印 `[backup-svc] FAILED` 到 stderr 并 **60 秒后快速重试**（而非静默吞掉等下一轮），便于运维通过容器日志及时感知。

```bash
# 查看备份服务状态
docker compose ps backup

# 手动触发一次备份（不等待每日计划）
docker compose run --rm backup
# 或直接执行脚本：
BACKUP_DIR=/tmp/unisense-backup MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 \
MYSQL_USER=unisense MYSQL_PASSWORD=test MYSQL_DATABASE=unisense \
./backend/scripts/backup.sh

# 查看备份产物
docker compose exec backup ls -lh /backups
```

产物：`/backups/mysql_YYYYmmdd_HHMMSS.sql.gz`（含表结构 + 数据 + 触发器 + 存储过程）。

### 1b. MySQL binlog 增量（自动，每 5 分钟）

由 `docker-compose.yml` 的 `binlog-backup` 服务执行；mysql 容器已开启 binlog
（ROW 格式，`--log-bin=/var/lib/mysql-binlog/binlog`，独立卷 `unisense_binlog`）：

```bash
# 查看增量备份服务状态
docker compose ps binlog-backup

# 手动触发一次增量归档
docker compose run --rm binlog-backup
# 或直接执行脚本：
BINLOG_SRC=/tmp BINLOG_DIR=/tmp/binlog MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 \
MYSQL_USER=unisense MYSQL_PASSWORD=test ./backend/scripts/backup_binlog.sh

# 查看增量归档（按日分目录 + 位置记录）
docker compose exec binlog-backup ls -lhR /backups/binlog
```

产物：`/backups/binlog/<YYYYmmdd>/binlog.*`（归档的 binlog 文件，按日分目录）+ 
`/backups/binlog/position.txt`（最近归档位置 File/Pos，供恢复定位增量起点）。

> **首次启用注意**：binlog 自 mysql 容器重启（开启 binlog）时刻才开始积累。
> 作为恢复基线，开启后应立即手动执行一次全量备份
> （`docker compose run --rm backup`），恢复链 = 该全量 + 之后全部 binlog。

### 2. Neo4j（手动/另行编排）

```bash
docker compose exec neo4j \
  bin/neo4j-admin database dump neo4j --to-path=/data/dumps
```

### 3. Elasticsearch（手动/另行编排）

```bash
curl -X PUT "http://localhost:19200/_snapshot/unisense_backup" \
  -H 'Content-Type: application/json' \
  -d '{"type":"fs","settings":{"location":"/usr/share/elasticsearch/backup"}}'
curl -X PUT "http://localhost:19200/_snapshot/unisense_backup/snap_$(date +%Y%m%d)"
```

---

## 三、恢复操作

### 场景 1：MySQL 数据丢失 / 损坏（最高频，含 binlog 回放）

```bash
cd /System/Volumes/Data/data/GitCode/Unisense

# 1) 找到最新全量备份 + 其后的 binlog 增量（恢复基线 = 全量时刻）
docker compose exec backup ls -lt /backups | head
docker compose exec binlog-backup ls -lhR /backups/binlog | tail -30

# 2) 停掉后端（避免写入旧库）
docker compose stop backend worker

# 3) 恢复最新全量备份到 MySQL
docker compose exec -T mysql sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" unisense' \
  < <(docker compose exec -T backup sh -c \
     'exec gunzip -c /backups/mysql_LATEST.sql.gz')

# 4) 回放全量备份时刻之后的 binlog 增量（按文件序，先 `mysqlbinlog` 合并再执行）
#    恢复起点：全量备份完成时的 binlog 文件/位置（见 /backups/binlog/position.txt 或备份脚本日志）
docker compose exec -T binlog-backup sh -c \
  'cd /backups/binlog && \
   find . -type f -name "binlog.*" | sort | \
   xargs -I{} sh -c "mysqlbinlog -h mysql -u unisense -p$MYSQL_PASSWORD --base64-output=DECODE-ROWS --stop-never-pid-file=/tmp/nope {} | mysql -h mysql -u unisense -p$MYSQL_PASSWORD unisense"'

# 5) 校验数据（表数量、关键表行数，与故障前一致）
docker compose exec mysql mysql -uunisense -ptest unisense -e \
  'SHOW TABLES; SELECT COUNT(*) FROM metric; SELECT COUNT(*) FROM audit_log;'

# 6) 重启后端
docker compose start backend worker
```

> 说明：步骤 4 按文件名升序（binlog.NNNNNN）回放即可覆盖从全量时刻之后的全部增量。
> 若已知具体恢复时间点，可用 `mysqlbinlog --start-datetime="..." --stop-datetime="..."` 精确到点回放。
> binlog 回放需在恢复目标库已禁用（或后端已停止）时执行，避免重复写入。

### 场景 2：全栈重建（MySQL 卷损坏需重建）

```bash
docker compose down
# 保留 unisense_backups 卷，删除损坏的 mysql_data
docker volume rm unisense_mysql_data
docker compose up -d mysql
# 等待 healthy 后按场景 1 恢复数据
```

### 场景 3：Neo4j / ES 重建

Neo4j 与 ES 均可由业务数据重建（血缘边来自 MySQL lineage_edge，检索索引来自
catalog/metric 表），非权威数据源。重建顺序：

1. 恢复 MySQL（场景 1）
2. 启动 Neo4j/ES，触发一次全量重建（血缘边重写图、索引重建）

---

## 四、恢复演练（建议每季度一次）

| 步骤 | 操作 | 验收 |
|------|------|------|
| 1 | 在测试环境执行场景 1 完整恢复（含 binlog 回放）| 表结构与数据一致 |
| 2 | 校验 `metric`/`audit_log`/`quality_event` 行数 | COUNT 一致 |
| 3 | 制造故障后按场景 1 恢复，验证 binlog 增量确实覆盖全量之后的变更 | 故障前最后写入的数据恢复成功 |
| 4 | 记录 RTO（从停服到恢复可用）| ≤ 4h |

## 五、待办（进一步提升）

- **异地存储**：将 `unisense_backups` 卷定时转存至对象存储（MinIO/OSS）或异地 NFS
- **自动恢复演练**：把演练流程接入 CI，季度自动执行
- **binlog 消费式归档**：如需审计级细粒度，可引入 canal/debezium 将 binlog 解析为可检索事件流

## 附：相关文件

- 备份脚本：`backend/scripts/backup.sh`（全量）、`backend/scripts/backup_binlog.sh`（增量）
- 编排服务：`docker-compose.yml` 中 `backup`（每日全量）与 `binlog-backup`（每 5 分钟增量）
- 数据卷：`unisense_backups`（备份产物）、`unisense_binlog`（mysql binlog，只读挂载给增量备份）
