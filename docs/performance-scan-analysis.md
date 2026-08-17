# AeroFoil 超大游戏库性能分析(扫描 / 识别 / 重建 / 并发)

> 分析范围:10k–100k 文件规模的游戏库,可能位于 NAS/网络挂载;并发来源包括 32 个
> waitress 工作线程、2 小时定时扫描、watchdog 文件监视、下载管理器、Tinfoil/CyberFoil
> 商店客户端。方法:三个子系统的代码审计(逐条带 file:line 证据)+ 2 万文件合成库实测。
>
> 生产环境已观测到的对应故障:`database is locked` 中断扫库、扫库后同批文件反复
> 重新入库、Web UI 卡死、`Skipping in-memory shop sections cache (15747019 bytes > 4194304 bytes)`。

## 0. 实测基准(2 万文件 / 1 万 title,本地盘,空文件,无 keys)

| 环节 | 首次 | 稳态(无变化) | 备注 |
|---|---|---|---|
| 文件发现扫描 `scan_library_path` | 16.0s | **0.30s** | 增量比对有效;但目录仍全量遍历 |
| 识别(文件名回退) | 66.5s(3.3ms/文件,纯 DB/CPU) | 0.02s | **有 keys 时稳态不会收敛**,见 F2 |
| `add_missing_apps_to_db` | 4.4s | 同量级 | 每 title 开 2+N 个 SQLite 连接 |
| `update_titles` 全表重算 | 20.2s | **20.9s(零变化也全量)** | 每次重建必跑 |
| `generate_library` | 7.5s | 0.20s(缓存命中) | 缓存失效条件过宽,见 F13 |
| TitleDB 加载 | 4.2s,**+420MB RSS** | 每次重建后 30s 卸载、下次重载 | US.en.json 89MB + cnmts ~100MB |
| 2 万条 INFO 日志(仅格式化+写出) | 0.8s | — | 真实环境经 Docker 管道更慢 |
| `file_exists_in_db` 单次 | 0.39ms | — | watchdog 海量事件路径逐文件调用 |

真实环境为空文件基准的放大版:识别涉及打开容器 + AES-XTS 解密头部(NSP 约 10 次
散列 seek / 数百 KB;XCI 逐 NCA 解密头),网络挂载单文件 50ms–2s。

---

## 1. P0 — 直接造成生产故障

### F1 SQLite 并发配置缺失(`database is locked` 的根因)
- `app/db.py:663-670`:连接钩子只设 `PRAGMA foreign_keys=ON`。**无 WAL、无
  busy_timeout、无 synchronous 配置**;实测数据库 `journal_mode=delete`。
- 全仓库没有任何 `SQLALCHEMY_ENGINE_OPTIONS`(`app/app.py:1845` 附近),pysqlite
  默认 `timeout=5.0`——这正是生产日志中"约 5 秒后报 locked"的来源。
- 连接池默认 `pool_size=5, max_overflow=10`(共 15),对着 32 waitress 线程 +
  4 调度线程 + watchdog + debounce + 下载线程,还会触发 `QueuePool limit` 超时。
- 讽刺的是 `app/titles.py:226-228` 打开自己的索引库时就用了 `timeout=30` +
  `synchronous=NORMAL`,主库反而没有。

DELETE 日志模式下:写者持 RESERVED 直到提交、提交需 EXCLUSIVE 会被任意读者阻塞,
读者也会被写者阻塞 → 5 秒等不到即抛错;等待期间 32 个请求线程排队 → UI 卡死。

### F2 识别的"永久重试陷阱"(扫完又扫的第二个来源)
- `app/library.py:320-334`:候选谓词为 `OR(identified=False, identification_type
  ='filename'[有 keys 时], orphaned)`。
- `identification_attempts` / `last_attempt` 在 `library.py:484-485` **只写不读**,
  没有任何退避——损坏/缺 keys 的文件每 2 小时被完整重新打开解密一次,永远如此。
- **孤儿陷阱**:`titles.py:1237` 的 `identify_appId()` 在 cnmts 索引缺失或未知
  app_id 时返回 `(None, None)`;`library.py:417/440` 把它过滤掉后,`library.py:470`
  仍标记 `identified=True` 但 **0 条 app_files 关联** → 该文件永远命中 `orphaned`
  条件,每轮全量重识别。库越大、非标准转储越多,每轮无效 I/O 越多(2000 个此类
  文件 × 0.5s ≈ 每周期 17 分钟纯随机网络 I/O,零状态收益)。
- 有 keys 时所有 `identification_type='filename'` 的文件每轮重试(成功会升级为
  `cnmt` 退出;持续失败则并入上一条永久循环)。

### F3 长写事务横跨慢速文件 I/O(持锁分钟级)
- `app/library.py:27` `_IDENTIFY_COMMIT_INTERVAL=50`:识别循环在**一个写事务内**
  连续处理 50 个文件,每个文件都做容器打开+解密(`library.py:415`),事务在首次
  autoflush(`library.py:419`)就已持有写锁 → 网络挂载上一个事务持锁数分钟,
  期间所有其它写者 5 秒超时报错(与 F1 叠加即生产故障)。
- 入库路径同理:`library.py:222` 的 `get_file_info` 在事务内执行,每 100 文件才
  提交(`library.py:238-241`)。生产日志中 "Getting file info (n/250)" 后紧跟
  locked 报错、整批回滚、下周期重加,就是这条链。

### F4 锁只保护标志位,不保护工作——多个全量任务可并发写库
- `scan_lock` / `library_rebuild_lock` / `titledb_update_lock`(`app.py:457-469`)
  都只包住 bool/dict 赋值;`_run_post_library_change`(`app.py:6536-6587`)取锁改
  标志后**立即释放**,重建本体无锁。
- 可同时写库的组合(均已确认调用链):
  1. 下载管理器每 30 秒 / 每 5 分钟直接调 `scan_cb()`(`manager.py:1531-1535`,
     `app.py:283-304`),**完全无视 scan_lock**;
  2. 请求线程直接调 `check_completed_downloads(scan_cb=scan_library)`
     (`app.py:4395,4402`);
  3. `app.py:4377` **绕过 debounce 直接**调 `_run_post_library_change()`,可与
     debounce 线程的重建并发;
  4. 维护任务只检查转换标志不检查扫描(`app.py:397-420`),且与扫描任务同为
     `run_first=True` → **启动时必然并发**;
  5. watchdog 观察者线程逐文件删除、逐文件 commit(`file_watcher.py:145`,
     `db.py:1126-1140`)。
- 另有 TOCTOU:`app.py:6606-6611` 读 `scan_in_progress` 在锁内、置位在锁外,
  两个并发 POST 都能通过检查。

### F5 watchdog 海量事件 O(E²) stat 风暴 + 可致监视线程静默死亡
- `file_watcher.py:152`:**每个事件**都同步调 `_check_file_stability()`,后者遍历
  全部 `tracked_files` 并对每项做 2 次 stat(`:103-121`)——一次移入 1 万文件
  ≈ **10⁸ 次 stat**(NFS 上数小时),同文件的 debounce 路径(`:150`)被这行完全废掉。
- `file_watcher.py:94` `os.path.getsize()` **无异常保护**:事件分发时文件已被移走
  即抛 FileNotFoundError,watchdog emitter 线程终止,**此后整个进程不再监视文件
  变化且无任何日志**。海量移动场景几乎必触发。
- 删除事件逐个回调(`:144-145`)→ 每删一个文件 4 条查询 + 1 次 commit。

---

## 2. P1 — 每周期 / 每请求的全量支出

### F6 `update_titles`:每次重建无条件全表重算(实测 20 秒/万 title)
`library.py:664-761`:批 500 遍历**所有** title,无脏跟踪;每 title 行都被改写
(即使值没变),全部 flush。`_sync_apps_owned_flags`(`library.py:1387-1404`)对
**整个 apps 表**执行相关子查询 UPDATE——SQLite 没有"值未变跳过写"优化,每周期
全表页面重写 + 日志膨胀。每次重建触发(约 18 个调用点)。

### F7 `add_missing_apps_to_db`:每 title 2+N 个 SQLite 连接,每周期 3–5 万次
`library.py:586-607`:每 title 调 `get_all_existing_versions`(1 连接)+
`get_all_existing_dlc`(1 连接)+ 每 DLC 一次 `get_all_app_existing_versions`。
`titles.py:1293/1542/1581/1619` 每次都是 `sqlite3.connect → PRAGMA → 查询 → close`,
无池化无复用。此外它把 TitleDB 已知的所有版本/DLC(不论是否拥有)插入 apps 表,
是 apps 表膨胀的源头(反过来放大 F6/F10)。

### F8 `remove_titles_without_owned_apps`:教科书式 N+1
`db.py:1018-1038`:加载全部 Titles ORM 对象后逐个 `has_owned_apps`,而后者还先
按 title_id **重查一遍调用者已持有的对象** → 2×T 次往返(万 title 即 1.6 万次)。
每次重建开头必跑(`library.py:669`)。可写成单条 `DELETE ... WHERE NOT EXISTS`。

### F9 全量 os.walk 每 2 小时一次,无任何 mtime 短路
`library.py:120-125` 干净的生成器(无排序、无逐文件 stat)——问题在缺失的机制:
`Libraries.last_scan` 在 `library.py:306` 写入后**全仓库无人读取**。网络挂载上
100k 文件 ≈ 1 万+ 次 readdir 往返(50–200 秒起步),即使什么都没变。
`scan_library` 串行遍历所有库(`app.py:6633-6637`)。

### F10 状态 token 指纹:18 个聚合子查询、含全表 LENGTH 表达式、TTL 仅 1 秒
`library.py:782-839`:`SUM((LENGTH(filepath)+LENGTH(filename)+size)*((id%131)+1))`
之类的表达式聚合无法用索引,对 files/apps/titles/app_files 全表扫描。
TTL `_LIBRARY_STATE_TOKEN_CACHE_TTL_S=1.0`(`library.py:39`),且
`is_library_unchanged` 用 `force_refresh=True` 绕过缓存(`:886`)。它挂在商店
请求路径(`app.py:560/606/630/5981`)——持续流量下**每秒全表扫描一次**。

### F11 `/api/titles` 默认排序全表物化 + 万次 TitleDB 查询
`app.py:5503-5531`:默认 `title_asc` 走 `use_name_sort` → `query.order_by(None)
.all()` 加载**全部**匹配行,再对每个 title/DLC 调 `get_game_info`,Python 排序后
才切页。`per_page` 上限 200 帮不上忙——工作量发生在切片之前。非名称排序路径
(`:5533`)有正确的 offset/limit,只是不是默认。**每次打开库页面/改筛选都触发**。
结尾还无条件调 `_get_discovery_sections`(`:5643`),把 F12 拖进同一请求。

### F12 15MB 商店缓存进入"比没有缓存更糟"的循环
- `SHOP_SECTIONS_MAX_IN_MEMORY_BYTES` 默认 **4MiB**(`app.py:910`),超限后内存缓存
  置 None(`:967-983`)。之后每个请求:磁盘 `json.load` 15MB(`:6195`)→ 再
  `json.dumps` 15MB 只为估算大小再次发现超限(`:950-954,966`)→ **顺手清空加密
  响应缓存**(`:983`)→ 全量 compress+encrypt(`:661`)。加密缓存结构上永远无法
  命中(清空发生在写入之前)。
- payload 之所以 15MB:`SHOP_SECTIONS_ALL_ITEMS_CAP` 默认 `None`(`:908`),`all`
  段含全部条目且与 new/recommended/updates/dlc 段重复。
- Web(limit=50)与 CyberFoil(limit=-1)两个 cache key 共用**同一个磁盘文件**
  (`:6166,6198`),交替访问互相打爆,每次 miss 全量重建 + 15MB 落盘;重建只预热
  50 的 key(`:6581`),CyberFoil 首请求永远全量重建。

### F13 `generate_library` 缓存失效条件过宽 + 全量重建成本
`library.py:870-887`:token 含 `CONFIG_FILE` mtime——**UI 里改任何设置都会招致下
次全量重建**(万 title 实测 7.5s + 万次 TitleDB 连接,`:990`);任何新增一个文件
也全量重建。而 TitleDB 更新反而**不在** token 里,与 shop/metadata 缓存(用
titledb-aware token)失效条件不一致,元数据可能漂移。

### F14 TitleDB 内存与重载循环:每次 0.4–0.5GB、活跃实例反复加载
`titles.py:999-1036`:descriptions(US.en.json,89MB)与截图 URL 为 **TitleDB 全
量条目**(数万个,而非仅库内游戏)建 dict 常驻;`versions.txt` 同样全量进内存。
`unload_titledb` 是 `@debounce(30)`(`:1105`)→ 重建后 30 秒卸载并清空查询缓存
(`:1143-1146`),下一个请求又全量 `json.load` 重来。活跃实例上这个 4 秒 + 0.4GB
的加载循环持续发生。(cnmts/versions/region titles 已正确转为 SQLite 索引按需查,
这部分设计是对的。)

### F15 每请求同步写库(商店流量放大器)
- `app.py:2195/2262-2268`:`before_request` 里 `_touch_client` 对**每个**带用户名
  的商店请求更新 user 行并 commit,无去重;失败静默吞掉,表现为纯延迟。
- `db.py:423`:access_events 缓冲的 flush 在**请求线程**内联执行(≥64 条或 ≥1 秒),
  每秒有一个倒霉请求承担 commit;扫库持锁时该请求卡满 5 秒。下载计数 flush 同构
  (`db.py:344-378`,且循环内逐 file_id 单条 UPDATE)。
- 两者都 commit **共享的请求级 session**,会把请求中其它未提交的 ORM 变更连带
  提交/回滚(`db.py:312,315`;`app.py:2268,2272`)。

### F16 access_events:新装实例零索引、无保留策略
- 模型无索引声明(`db.py:233-252`);索引只存在于 Alembic 迁移
  (`migrations/versions/2c9d2a6e3b41:37-38`)。而新装库走 `db.create_all()` +
  `stamp head`(`db.py:673-677`),**迁移被跳过,索引永远不会建**(实测确认零索引)。
- `ORDER BY at DESC LIMIT n` 变成全表扫描+排序;活动页上限 1 万条、CSV 导出更大。
- **没有任何自动清理**;手动清空是单条无界 DELETE,百万行时持 EXCLUSIVE 锁锁死全库。
- 另:管理员 GET 活动页会触发 `flush(force=True)`——读接口执行写提交(`db.py:430`)。

### F17 索引"双轨制":create_all 与 Alembic 各缺一半
`ensure_performance_schema`(`db.py:544-583`)只补 5 个索引;模型上的
`idx_files_library_id/filename/identified`、`idx_apps_owned/app_id` 不在迁移中——
**老库升级后没有这些索引**(`iter_library_file_paths` 等变全表扫描);新库有它们
却缺 access_events 的索引。两条路径都得不到完整索引集。

### F18 下载任务 O(N²):未过滤的聚合子查询 × 每 title 一次
`db.py:930-966` `get_all_title_apps` 的 size 子查询对**整个** `app_files⋈files`
做 GROUP BY 后才按 title 过滤;`manager.py:937-949` 每 5 分钟对每个 title 调一次
→ 2 万 title = 2 万次全量聚合。同构问题在 `manager.py:1297`、`library.py:765`。

---

## 3. P2 — 次级但真实

| # | 问题 | 位置 |
|---|---|---|
| F19 | 识别候选查询:OR 谓词无法用索引,全表扫 ×2/周期;`identification_type` 无索引;count 仅为日志服务却全表+相关 EXISTS | `library.py:320-334,358`;`db.py:900-905` |
| F20 | 识别循环 N+1:批查 id 后逐个 `session.get`(expunge 后必回库);`file not in existing_app.files` 触发整集合加载 | `library.py:398,446,452` |
| F21 | 调度器在**派发时**就排下次运行:超 2 小时的扫描会自我重叠;`update_titledb_job` 无 in-progress 防护;4 worker 池可被长任务占满 | `scheduler.py:76-87,13` |
| F22 | `meta_only` 能力探测不记忆:nsz fork 不支持时**每个文件**付一次失败 open + 全量容器解析(5–20× I/O) | `titles.py:1205-1211` |
| F23 | 6 小时 missing-files sweep 对全表逐行 `os.path.exists`(与扫描信息完全重复) | `db.py:1142-1190` |
| F24 | 媒体缓存索引每次重建清空 → 之后首万次图标请求各自 listdir + 线性扫万条目(~10⁸ 次操作) | `app.py:224-229,6575` |
| F25 | metadata 缓存**命中**即深拷贝 O(T)(万条 name map + 全部 genre 集合) | `app.py:804-812` |
| F26 | 每文件 INFO 日志 + f-string 先求值:全量扫描 20 万+ 行(实测 0.8s/2 万行,Docker 管道更差) | `library.py:220,414,431` |
| F27 | `is_supported_content_path` 函数内 import + 每候选文件 12 次字符串分配;walk 对非游戏文件也全量调用 | `utils.py:126-150` |
| F28 | 扫描将全部路径物化为 set(100k ≈ 25MB 常驻整个 walk 期间) | `library.py:267` |
| F29 | TitleDB 查询 LRU 上限 4096 < 现实库规模,且 FIFO 不重排,大库上命中率趋零;TitleDB 卸载时全清 | `titles.py:134,1320-1324` |
| F30 | 每次 TitleDB 查询走 `load_settings()` → 每次全量 pass 1 万+ 次 stat | `titles.py:1388,206-209` |
| F31 | 死代码:`get_files_to_identify`(全量三次物化版本)及配套 helper 仍在,误用即灾难 | `library.py:336-341`;`db.py:809-813,860` |

---

## 4. 生产故障复盘(因果链)

```
[每2h] TitleDB更新+扫库 ──┐
[每30s/5min] 下载任务直调 scan ──┼── 多个写者并发(F4)
[watchdog] 逐文件删除+commit ──┘        │
                                        ▼
     识别/入库长事务持锁分钟级(F3) ←→ DELETE journal + 5s timeout(F1)
                                        │
                            "database is locked" → 整个扫库任务中止
                                        │
                     本批未提交文件回滚丢失 → 下周期重新 "Getting file info"(观测现象)
                                        │
     同时:15MB shop payload 超 4MB 限制(F12)→ 每请求 15MB 读+序列化+加密
     + /api/titles 全表物化(F11)+ 每请求同步写(F15)
                                        ▼
                        32 线程全部堵在锁/IO 上 → UI 卡死(观测现象)
```

## 5. 修复路线图

**第一批 · 止血(小改动,直接消除生产故障)**
1. WAL + busy_timeout:`db.py` 连接钩子加 `journal_mode=WAL`、`synchronous=NORMAL`、
   `busy_timeout=30000`;`app.py` 设 `SQLALCHEMY_ENGINE_OPTIONS`
   (`connect_args={'timeout':30}`, `pool_size=20`, `max_overflow=30`)。
2. 识别/入库 I/O 移出写事务:先收集结果,再开短事务批量写;失败只丢单文件并记录。
3. 锁收敛:`_run_post_library_change` 全程持有重建锁并对重入直接返回;下载管理器与
   `app.py:4377` 一律走带防护的入口;修 `app.py:6611` TOCTOU。
4. shop 缓存三处一行级修复:磁盘命中路径不再重估大小、超限时不清 encrypted 缓存、
   `SHOP_SECTIONS_MAX_IN_MEMORY_BYTES` 默认提到 64MB;web/CyberFoil 分离磁盘文件。
5. `file_watcher.py:94` 加 try/except;`:152` 删除(信任 debounce 路径)。

**第二批 · 增量化(消除每周期全量支出)**
6. 识别退避:启用 `identification_attempts/last_attempt` 过滤(指数退避+上限);
   修复孤儿陷阱(`nb_content==0` 时不置 `identified=True`,或标记为永久失败态)。
7. `update_titles`/`_sync_apps_owned_flags`/`add_missing_apps_to_db` 加脏跟踪:仅
   处理本次识别/删除涉及的 title;`remove_titles_without_owned_apps` 改单条
   `DELETE WHERE NOT EXISTS`。
8. `/api/titles` 默认排序下推 SQL(COLLATE NOCASE 或预计算排序列)。
9. TitleDB 索引连接线程本地复用;`identify_appId` 加记忆化;LRU 提到 ≥32k 并改
   真 LRU。
10. 索引补齐(双轨都补):`identification_type`、access_events `(at)`/`(kind)`;
    access_events 加保留策略(定期分批删除)。
11. 状态 token 换成显式版本计数器(写路径 bump),TTL 提到 30s+。

**第三批 · 结构性(可选)**
12. watcher 事件进队列、后台单线程批量消费(批 stat、批删除、单事务)。
13. 扫描 mtime 短路:目录 mtime + `last_scan` 比对,只深入变化子树。
14. TitleDB 描述/图片索引化(SQLite),取消 0.4GB 常驻与 30s 卸载循环。
15. access_events/下载计数移到专用写线程,请求线程零同步写。
16. shop `all` 段设默认上限,超大库分页/流式输出。

---

*基准测试脚本:合成 2 万文件库逐环节计时;详见分支内提交历史与本文件第 0 节。*
*本分析在 `perf/scan-analysis` 分支;修复未在本分支实施。*
