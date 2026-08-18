# AeroFoil Fork 交接文档(handover)

> 交接对象:接管 `freefrank/AeroFoil` 的本地 agent。
> 本文档在 `fork-notes` 分支,**不要合入 master**(master 是上游贡献的基础,必须保持干净)。
> 交接时间点:master @ `a3513f9`。

## 1. 仓库与分支结构

| 分支 | 内容 | 注意 |
|---|---|---|
| `master` | 全部成果:i18n + 性能修复 + 上游 92 commit 同步 + Docker workflow。**不含任何 fork 内部文档** | 上游 PR 从这里 cherry-pick |
| `fork-notes` | fork 内部工作文档:性能分析报告、PR 跟踪表、上游 issue 草稿、基准脚本、本文档 | 永不合入 master/上游 |
| `perf/scan-analysis` | 历史分支(12 个修复的原始 commit),已全部合入 master | 可删,留作历史也行 |
| `claude/project-ui-i18n-config-gjm13g` | 历史分支(i18n 开发),已合入 master | 同上 |
| remote `upstream` | `https://github.com/luketanti/aerofoil.git` | 已同步至其 2026-08 状态 |

关键 commit(均在 master):
- `74d3643` i18n 合入 · `f1828a6` 性能修复合入 · `993f442` 上游合并 · `9232b0a` 新文案补翻 · `d7f0bdf` 恢复逐文件日志 · `a3513f9` 移除 fork 文档
- 12 个性能修复 topic commit 用 `git log --grep="^PR-N:"` 定位(N=1..12),与 `docs/performance-fix-plan.md`(本分支)的表格一一对应

## 2. 已完成的三大块

### 2.1 i18n(Flask-Babel)
- 899 条 msgid,zh_Hans 全量翻译;目录在 `app/translations/`,`babel.cfg` 在仓库根
- 语言协商:cookie(`aerofoil_lang`,导航栏地球下拉设置)→ Accept-Language → en
- **代码约定**(改 UI 必守):HTML 文本 `{{ _('...') }}`;内联 JS 字符串 `{{ _('...')|tojson }}`;JS 模板字面量内 `${ {{ _('...')|tojson }} }`;运行时变量用 `{name}` 占位 + `.replace('{name}', v)` — **禁止**无 kwargs 的 `%(name)s`(Jinja newstyle gettext 会无条件 `% variables`,渲染时直接 KeyError)
- 文案变更后的流程:`pybabel extract -F babel.cfg -o messages.pot .` → `pybabel update -i messages.pot -d app/translations` → 补翻 → `pybabel compile -d app/translations`;.mo 已入库,Docker 构建时也会重编译

### 2.2 性能修复(12 项,详见本分支 docs/performance-fix-plan.md + performance-scan-analysis.md)
- 实测:入库 16s→3.2s、识别 66.5s→37.3s、update_titles 20.2s→0.5s(稳态 6ms)、/api/titles 2.7s→0.32s(2 万文件基准)
- 基准脚本已存到本分支 `tools/perf/bench_scan.py`(合成 2 万文件库逐环节计时)和 `tools/perf/render_smoke.py`(双语言全页渲染冒烟);脚本里的绝对路径 `sys.path.insert(0, '/home/user/AeroFoil')` 和 scratchpad 路径需按本地环境调整
- 一处**用户偏好回退**:逐文件日志保持 INFO(`d7f0bdf`),不要再降级

### 2.3 上游同步 + Docker 发布
- 已合并上游 92 commit(金手指管理器、ESRB 分级、虚拟流等),306 测试全过
- 数据库迁移全自动(启动时 alembic upgrade + 幂等 ALTER/索引补齐,先自动备份)

## 3. Docker 发布流程(已跑通)

- 镜像:`freefrank/aerofoil`;已发布 `2.7.0-dev` = `latest`(amd64+arm64)
- 凭据:GitHub Environment **`Docker Build`** 里的 `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN`(workflow 已声明 `environment: Docker Build`,别删这行)
- 触发方式:① 推 `v*.*.*` tag(本地可以推 tag,云端推不了 — 本地 agent 直接 `git tag v2.7.0 && git push origin v2.7.0` 即可);② Actions 页面/API 手动 dispatch `docker-image.yml`,输入 version(不带 v)
- **标签策略**:`latest` 每次发布都动(含 `-dev` 预发布);`stable` + `2` + `2.7` 只在纯版本号(如 `2.7.0`)时生成。用户后续想发稳定版时用 `2.7.0` 触发即可
- 推 `dev` 分支 → 自动发 `:dev` 标签(docker-image-dev.yml)
- 注意:云端曾建过本地 tag `v2.7.0-dev` 未推成(代理限制),本地仓库如果没有此 tag 无需理会

## 4. 待办(按优先级)

1. **发上游 issue(第一批,就绪)**:本分支 `docs/upstream-issues/post-issues.sh` 用 gh CLI 一键发 5 条(需 `gh auth login`);或从 `docs/upstream-issues-batch1.md` 网页粘贴。标题已按上游 tracker 惯例改为 `[Bug] 症状 (原因)` 式;issue-5 引用了上游 #131 作先例
2. **issue 有回应后提 PR**:从对应 topic commit cherry-pick 到基于 `upstream/master` 的新分支。**必须用 `git cherry-pick -n` 然后剔除 `docs/` 路径的变更**(topic commit 里带有 fix-plan 跟踪文档的 hunk,不能进上游 PR)。PR 描述模板:症状 → 根因(file:line)→ 修复 → 实测前后数据(数据在 performance-fix-plan.md 各节)
3. **第二批 issue**:候选清单在 `docs/upstream-issues-batch1.md` 末尾("Batch 2 candidates",已附 commit 链接),等第一批有反馈后再发
4. **i18n 上游化**(可选):给上游发 issue/discussion 问是否想要 i18n,附中文界面截图佐证,同意后分阶段 PR(基础设施→模板→翻译)。9000 行 diff 不要一次性甩
5. **第三批结构性优化**(未实施,见 performance-scan-analysis.md §5 Batch 3):watcher 队列化、扫描 mtime 短路、TitleDB 描述索引化(0.4GB 常驻内存问题)、专用写线程、shop `all` 段上限

## 5. 硬性约定(用户要求)

- **commit 信息里不加任何 Claude/AI 署名**(no Co-Authored-By 等)
- 每个阶段性工作独立 commit + push,用户要看得到过程记录
- fork 内部文档(分析、跟踪、草稿)只进 `fork-notes`,不进 master
- 用户读中文,交流用中文;代码/commit/上游 issue 用英文

## 6. 验证基线

- `python -m pytest tests/` → **306 passed**(上游合并后的完整套件)
- 全页渲染冒烟:`tools/perf/render_smoke.py`(en + zh_Hans 各 12 页,含 /cheats /profile)
- Jinja 全模板编译:`python3 -c "from app.app import app; import os; [app.jinja_env.get_template(t) for t in os.listdir('app/templates') if t.endswith('.html')]"`
- pybabel extract 无告警;`.po` 无未翻译条目(用 babel.messages.pofile 检查 `m.string` 为空的条目)

## 7. 已知坑速查

- Jinja gettext 无条件 `%` 插值 → msgid 含裸 `%` 或无 kwargs 的 `%(x)s` 会在渲染时崩(见 §2.1 约定)
- WAL 模式会产生 `-wal`/`-shm` 文件;数据库不要放网络挂载
- 升级后回退旧版镜像需还原 config 目录里的 `.backup_*.db`
- `remove_titles_without_owned_apps` 必须用 NOT IN 反连接写法 — 相关子查询形式 SQLite 会选错索引(8.4s vs 0.03s),改动时留意
- Apps 表的 `title_id` FK 有 `ondelete=CASCADE`,titles 的裸 DELETE 是安全的
- upstream 的 nsz 依赖是 git pin(`requirements.txt`),某些环境编译不过,测试时可临时剔除(app 会懒加载降级)

## 8. 2026-08-17 本地接手后进展(master @ `cccbcfb`)

本地环境已接管(upstream remote、.venv、306→313 测试基线)。用户本地 Docker 实测驱动了一轮修复,全部已合入 master 并发布:

| commit | 内容 |
|---|---|
| `cbf6eac` | 启动/重建后后台预热 metadata+discovery 缓存(首屏不再冷) |
| `ef8a212` | 前端加载失败重试状态 + 游戏图片懒加载(新翻译 2 条) |
| `d5a6845` | 删除接口改脏范围同步 sweep + 后台防抖重建;删除 DB 清理批量化 |
| `d9f6be2` | 主库+TitleDB 索引 mmap(AEROFOIL_DB_MMAP_SIZE_MB,默认 256) |
| `60a1b41` | watcher 删除事件批量化(单事务,消除与 API 删除的逐文件竞争) |
| `8688cda` | 本地文件元数据解析结果持久化(data/cache/local_metadata_cache.json) |
| `4e62887` | **新功能**:备用元数据数据库(titles.metadata_fallbacks,主库缺条目时按序回退,设置页两个下拉;顺带修 prefer_english_metadata 被表单保存重置的 bug)(新翻译 6 条) |
| `cccbcfb` | update_titledb 加进程锁(修并发下载互写坏 cnmts.json,fresh install 必现) |

已发布镜像 tag:`v2.8.0-dev`(@d5a6845)、`v2.8.1-dev`(@60a1b41)、`v2.8.2-dev`(@8688cda)、`v2.8.3-dev`(@cccbcfb),均更新 `latest`。

待办变化:
- 上游 issue 第一批**仍未发**(用户要求等其本地测试完成后再发,勿擅自发)
- 以上修复中 `cccbcfb`(TitleDB 并发)、`60a1b41`(watcher)、`cbf6eac`(冷缓存)是上游 issue/PR 的新候选,可并入 batch 2/3
- 用户库实测发现:健身环"缺失"是亚版 update 文件(0100F6B011028800)带出的 title 行,非 bug

2026-08-18 增补(master @ `c0beae8`,已发布 `v2.8.4-dev`):
- `eac3532` docker/run.sh 的 chown -R 改为按顶层属主判断(修每次启动静默数分钟;上游同款问题,issue 候选)
- `7a2dbbf` 网页搜索匹配主库+备用库+英文库全部名字(CN 主库下搜英文名可命中)
- `c0beae8` shop sections 载荷新增 search_names 别名数组(供 CyberFoil 类客户端多语言搜索;Tinfoil 本体搜索不受服务器控制)
- 用户环境问题存档:其 compose 曾把 data 挂到 /data/titledb(错误路径),导致 TitleDB/缓存不持久、"每次启动 first time 下载"——已纠正为 ./data:/app/data。类似 issue 报告先查挂载。

挂起项:generate_library 全量重生成实测 54s(62520 apps,大头是含描述/截图的全量 JSON 序列化 + 每 base title 版本点查 + 名称 LRU(32768)小于库规模导致的抖动)。用户决定:便宜优化(缓存版本查询/调大 LRU/流式写)不做,增量化(按 title 缓存条目、脏范围拼装)挂起待后续,可与 batch-3 一起按上游 PR 标准实施。
