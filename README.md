# OSM 洪水分析水域面 API

按 OSM 关系号提交**逐次递增修订**的完整节点经纬度与无序 way（含 `outer` /
`inner` 角色），服务按节点引用拼接 way（允许线段反向），生成一个外环、至多
一个内环的 GeoJSON Polygon，并管理"当前供数面"的状态机。

技术栈：**FastAPI + Shapely + SQLite（WAL 模式）**，可 `docker compose` 启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档（可直接上传、取面）
open http://localhost:8000/docs
```

本地无 Docker 时：

```bash
pip install -r requirements.txt
FLOOD_DB_PATH=./flood.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/relations/{relation_id}/revisions` | 上传一个完整修订（修订号必须严格递增） |
| POST | `/relations/{relation_id}/revisions/{revision}/approve` | 核准一个仍为最新修订的待核版，使其供数 |
| GET  | `/relations/{relation_id}/revisions/{revision}` | 按修订号读取**原始输入 + 几何 + 状态** |
| GET  | `/relations/{relation_id}/revisions` | 列出该关系全部修订号与状态 |
| GET  | `/relations/{relation_id}/current` | 取当前供数面（最新有效修订） |
| GET  | `/health` | 健康检查 |

### 上传请求体

```json
{
  "revision": 1,
  "nodes": [
    {"id": 1, "lat": 0.0, "lon": 0.0},
    {"id": 2, "lat": 0.0, "lon": 1.0},
    {"id": 3, "lat": 1.0, "lon": 1.0},
    {"id": 4, "lat": 1.0, "lon": 0.0}
  ],
  "ways": [
    {"id": 10, "role": "outer", "node_refs": [1, 2, 3, 4, 1]}
  ]
}
```

约束：每次提交为该修订的**完整快照**；最多 **8 条 way、32 个节点**；way
无序，拼接时可反向遍历；`node_refs` 闭合方式沿用 OSM 惯例（首尾节点相同）。

## 校验与状态机

以下情况修订标记为 `invalid`，**原始输入完整保留**，响应中返回错误码及相关
way id，当前面**停止供数**（`/current` 返回 409）：

- `missing_nodes`：way 引用了未提交的节点
- `broken_ring`：单 way 未闭合、way 间断环、外环拆成多个连通分量
- `fork`：三条以上 way 端点汇聚，或 way 端点落在另一条 way 内部（T 型分叉）
- `self_intersection`：环自交 / 节点重复访问（8 字形等）
- `inner_ring_violation`：内环触碰外环边界、越过外环或位于外环之外
- `missing_outer`：没有 `outer` 角色的 way

停止供数期间，**上一个有效面仍可按修订号读取**，响应里的
`last_valid_revision` 指向它；后续出现有效修订后 `/current` 自动恢复供数。

## 发布前质检（交并比）

拓扑合法不等于坐标可信：节点整体漂移的修订会误导洪泛分析。因此每个几何
合法的修订在供数前，都会与**该关系最后供数面**计算交并比（IoU）：

- **首个有效版**（尚无比对基线）直接供数，`iou_ratio` 为 `null`；
- **IoU ≥ 0.5**：状态 `valid`，直接供数；
- **IoU < 0.5**：状态 `pending`（待核），**原始输入、几何与比值全部保留**，
  `/current` 继续提供旧面（HTTP 200），并以 `pending_revision` 指向待核修订。

待核期间再上传新修订，**仍以最后供数面为比较基线**（而非待核版）。

### 核准

`POST /relations/{relation_id}/revisions/{revision}/approve`（可在 `/docs`
中直接操作）将**仍为最新修订**的待核版置为 `approved` 并立即供数，此后它
成为新的质检基线：

- 核准**迟到的旧版**（已有更新修订）→ 409 `stale_revision`，供数不变；
- 核准 `valid` / `invalid` 版 → 409 `not_pending`（拓扑无效版**不得核准**）；
- 核准不存在的修订 → 404。

输入约束违例（超 8/32 上限、角色非法、修订号非递增等）返回 422 / 409，
**不落库**。

## 一致性、原子性与持久化

- SQLite 启用 **WAL + `synchronous=FULL`**：提交即 fsync，重启后数据可查；
  读不阻塞写，读者只能看到完整提交的修订。
- 每次写入在单个 `BEGIN IMMEDIATE` 事务内完成"递增检查 + 质检基线读取 +
  插入"，进程内再以锁串行化；存储失败整体回滚，**不会留下半份修订**。
  因此上传回执中的状态、IoU 比值与供数指针必然对应同一已提交状态。
- 核准的"是否仍是最新修订"检查与状态翻转同样在**单个事务**内提交；与新
  上传交错时，要么核准先生效（新上传转而与核准面比对），要么新上传先
  生效（核准报 409 冲突），**失败的核准不改变供数**。
- `/current` 与按修订号读取在同一个读事务内取"最新修订 + 供数面"，单次
  请求永远看到一致的已提交快照。
- 原始输入、几何、状态、IoU 比值同事务写入，因此并发上传与读取看到的
  同一修订必然一致。
- 数据库位于挂载卷 `/data/flood.db`（环境变量 `FLOOD_DB_PATH` 可改）。
- **旧库兼容**：质检功能上线前创建的库在首次打开时自动原地迁移（重建
  `revisions` 表以扩展状态约束并新增 `iou_ratio` 列），已有数据全部保留
  且可正常读取。

## 自测

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

覆盖：单/多 way 拼接与反向、内环挖洞、缺节点、断环、分叉、T 型分叉、自交、
内环触碰/越界、状态机恢复、修订号冲突、上下限校验、重启恢复、事务原子性、
并发上传/读取、IoU 质检门控（待核/核准/冲突/基线）、核准与上传交错、旧库
迁移、`/docs` 可用性。

## 示例

`examples/` 目录给出一个关系的连续修订序列，可直接在 `/docs` 中依次上传
体验状态机：`rev1` 有效正方形（直接供数）→ `rev2` 带内环 → `rev3` 缺节点
（invalid，停供）→ `rev4` 无序 way（恢复供数）→ `rev5` 坐标整体漂移
（IoU < 0.5，转待核，可核准）。
