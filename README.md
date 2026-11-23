# 考勤跨夜班与加班分段核算

面向制造企业薪酬核算员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18252 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“重算工时”会重新计算，同源输入复用批次。

## 业务模块

- 打卡采信：`/m1_ingest`、`/api/m1_ingest`
- 班次日历：`/m2_mapping`、`/api/m2_mapping`
- 跨夜配对：`/m3_versions`、`/api/m3_versions`
- 缺卡复核：`/m4_segments`、`/api/m4_segments`
- 工时切段：`/m5_calculate`、`/api/m5_calculate`
- 薪资对账：`/m6_reconcile`、`/api/m6_reconcile`
- 周期归档：`/m7_archive`、`/api/m7_archive`
- 社保申报：`/m8_declare`、`/api/m8_declare`、`POST /api/declarations`

## 核心规则

跨夜按班次归属日配对，休息区间先扣除，再按法定日期和工时阈值切分倍率。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因、对账与申报逐项对应。复核需提交当前版本，陈旧复核返回409。

## 社保缴费基数申报

在途工资（隔离待核项）不计入本次基数，登记为后续补报候补；已确认工资按规定期间申报。规则版本或计算参数变化时，先前申报作废并须重新核对；同一批次重跑不重复申报。未对平不得申报或导出；页面与导出使用同一申报计划。批次写入单事务，历史版本与原始文件均可恢复。详见 `docs/DATA-DICTIONARY.md`。

## 申报接口

```bash
# 提交当前批次为社保申报（同批重跑返回 unchanged，不重复申报）
curl -X POST localhost:18252/api/declarations -H 'Content-Type: application/json' -d '{}'
# 查看当前批次的逐项申报计划
curl localhost:18252/api/m8_declare/plan
# 导出申报结果 CSV（未对平返回 409）
curl -o declaration.csv localhost:18252/api/declarations/<declaration_id>/export.csv
# 复核申报：version 必须是最新值，否则 409
curl -X POST localhost:18252/api/declarations/<id>/review -H 'Content-Type: application/json' -d '{"version":1,"decision":"通过"}'
# 取回原始工资/打卡文件与历史版本
curl -o source.csv localhost:18252/api/runs/<run_id>/source.csv
curl localhost:18252/api/runs/<run_id>/revisions
```

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
