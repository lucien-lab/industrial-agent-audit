# 数据库账号与最小权限（P2-3 设计，未应用到生产）

## 现状与风险

Phase 1B 直接使用 `app_user` 连接数据库，该账号是 **超级用户**（`CREATE EXTENSION vector`
需要超级用户权限）。因此任何应用侧误操作或注入都能影响整个集群（其他数据库、角色、复制等）。

## 目标模型（三账号分离）

| 账号 | 用途 | 权限 |
| --- | --- | --- |
| `app_user`（现有） | 迁移/运维：`scripts/db.sh`、`scripts/migrate.sh`、备份恢复 | 库 owner + `CREATE EXTENSION`（pgvector 非 trusted 扩展，首次需超级用户）；不用于应用连接 |
| `iaa_app` | 只读应用（Phase 1C 查询接口） | `CONNECT`、`USAGE(public)`、`SELECT` 全部表/视图、`EXECUTE sensor_coverage()`；**无** INSERT/UPDATE/DELETE/DDL/TRUNCATE |
| `iaa_importer` | 数据导入任务 | 在 `iaa_app` 基础上增加 `INSERT/UPDATE/DELETE` 与序列 `USAGE`；仍无 DDL、无超级用户 |

迁移账号继续负责 DDL；应用与导入账号都不能建表、删表、`TRUNCATE` 或创建扩展。

## 部署步骤（仅在获得批准后执行）

```bash
# 1) 生成两个强密码（不要写进仓库）
APP_PW="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
IMP_PW="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"

# 2) 以迁移账号执行角色脚本（密码通过 psql 变量传入，不落盘）
psql "postgresql://app_user@127.0.0.1:5432/industrial_agent_audit" \
     -v app_password="$APP_PW" -v importer_password="$IMP_PW" \
     -v dbname=industrial_agent_audit \
     -f deploy/roles/01-roles.sql

# 3) 验证（只读）
psql "postgresql://iaa_app@127.0.0.1:5432/industrial_agent_audit" -c "SELECT count(*) FROM sensor_readings"
psql "postgresql://iaa_app@127.0.0.1:5432/industrial_agent_audit" -c "DELETE FROM data_gaps"   # 必须被拒绝

# 4) 切换应用连接串（.env 中 DATABASE_URL 改为 iaa_app；导入任务单独用 iaa_importer）
```

## 回滚

```sql
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM iaa_app, iaa_importer;
REVOKE ALL ON SCHEMA public FROM iaa_app, iaa_importer;
DROP OWNED BY iaa_app, iaa_importer;
DROP ROLE iaa_app, iaa_importer;
```

## 当前状态

- 本阶段**只在隔离数据库验证了权限边界**（见 `docs/phase1b_remediation.md` 的 P2-3 条目），
  **没有**在生产库创建角色或修改权限。
- 生产库仍使用 `app_user`；切换到最小权限账号属于运维变更，需单独批准窗口。
- 生产 PostgreSQL 已存在的 volume 中，角色密码存储在数据库内；`.env` 只是连接凭据副本，
  两者需同时更新（详见 README「修改数据库密码」）。
