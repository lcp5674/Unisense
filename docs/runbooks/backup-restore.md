# Unisense 备份与恢复 Runbook（RTO ≤ 4h / RPO ≤ 1d）

> 对齐等保 2.0 §8.1.6.1（备份与恢复）与 TD §18 韧性要求。
> 备份对象：MySQL（业务主库，权威数据）、Neo4j（血缘图，可重建）、
> Elasticsearch（检索索引，可重建）、审计归档（MinIO）。

---

## 一、备份策略总览

| 数据 | 方式 | 频率 | 保留 | 介质 |
|------|------|------|------|------|
| **MySQL** | `mysqldump --single-transaction`（逻辑备份，RPO≤1d） | 每日 02:00 | 7 天 | `unisense_backups` 卷 / 外部存储 |
| Neo4j | `neo4j-admin database dump` | 每日 | 7 天 | 节点本地 + 转存 |
| Elasticsearch | snapshot API → 仓库 | 每日 | 7 天 | ES snapshot 仓库 |
| 审计归档 | MinIO 对象（应用层已归档） | 持续 | 180 天 | MinIO 卷 |

> **RPO 说明**：MySQL 每日一次逻辑备份，极端情况下最多丢失 1 天数据。
> 若需 RPO 更低（分钟级），需引入 binlog 增量备份（后续迭代，见 §五）。

## 二、备份操作

### 1. MySQL（自动，每日 02:00）

由 `docker-compose.yml` 的 `backup` 服务执行：

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

### 场景 1：MySQL 数据丢失 / 损坏（最高频）

```bash
cd /System/Volumes/Data/data/GitCode/Unisense

# 1) 找到最新备份
docker compose exec backup ls -lt /backups | head

# 2) 停掉后端（避免写入旧库）
docker compose stop backend worker

# 3) 恢复最新备份到 MySQL
docker compose exec -T mysql sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" unisense' \
  < <(docker compose exec -T backup sh -c \
     'exec gunzip -c /backups/mysql_LATEST.sql.gz')

# 4) 校验数据（表数量、关键表行数）
docker compose exec mysql mysql -uunisense -ptest unisense -e \
  'SHOW TABLES; SELECT COUNT(*) FROM metric;'

# 5) 重启后端
docker compose start backend worker
```

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
| 1 | 在测试环境执行场景 1 完整恢复 | 表结构与数据一致 |
| 2 | 校验 `metric`/`audit_log`/`quality_event` 行数 | COUNT 一致 |
| 3 | 记录 RTO（从停服到恢复可用）| ≤ 4h |

---

## 五、待办（提升 RPO）

- **binlog 增量备份**：MySQL 开启 binlog + `mysqlbinlog` 增量归档，RPO 从 1d 降至分钟级
- **异地存储**：将 `unisense_backups` 卷定时转存至对象存储（MinIO/OSS）或异地 NFS
- **自动恢复演练**：把演练流程接入 CI，季度自动执行

---

## 附：相关文件

- 备份脚本：`backend/scripts/backup.sh`
- 编排服务：`docker-compose.yml` 中 `backup` 服务
- 数据卷：`unisense_backups`
