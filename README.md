# 超星课程回放字幕导出

适用于西安电子科技大学的超星录直播页面。使用有课程回放权限的个人账号登录，通过 HTTP 查询课表和下载字幕。首次安装请参阅 [安装说明](INSTALL.md)。

## 项目结构

```text
chaoxing-subtitles/
  download_date.py       按日期查询和批量导出入口
  download_live.py       HTTP 会话、回放解析及单回放下载
  search_subtitles.py    关键语句定位入口
  server.py             MCP 服务入口（已注册路径）
  export_worker.py       后台导出进程
  mcp_jobs.py            后台任务状态和进度
  export_storage.py     输出锁和历史字幕清理
  project_core.py       共享配置与错误类型
  subtitle_format.py    下载、清理、TXT 和定位共用的 VTT 解析器
  session_manager.py    会话检查、Cookie 更新及登录任务管理
  login_worker.py       专用 Edge 静默恢复和扫码等待
  manage_login.py       登录与续期命令行入口
  requirements.txt      依赖版本
  tests/                离线自动测试
  scripts/check_mcp.py   MCP 连接和流程检查
  .local/               本地登录态、专用浏览器配置、任务状态及锁（不提交）
  subtitles/            导出的字幕和结果清单（不提交）
```

## 环境

按 [安装说明](INSTALL.md) 创建或激活 Conda 环境，并在包含 `server.py` 的项目目录执行下列命令。

## 自动登录与按需续期

```powershell
python manage_login.py
```

先验证 `.local/auth.json` 中的现有会话；需要恢复时，使用 `.local/browser-profile/` 中的专用 Edge 配置尝试静默 SSO。已有的登录态会自动导入该专用配置。静默恢复不成功时打开登录窗口，由你扫码或完成验证；程序检测成功后会自动保存并关闭专用窗口，不需要手工复制 Cookie 或正常关闭录制窗口。

日常按日期导出及单回放下载会在任务前自动检查会话，保存服务器返回的更新 Cookie。任务中遇到明确的未登录响应（包括 HTTP 200 的“请先登录”页面）时，会恢复会话并将该请求重试一次。导出任务等待验证时显示 `waiting_login`，验证完成后继续原任务；关闭窗口或超时会报告失败并保留历史字幕。权限不足的 HTTP 403 不会被当作登录过期反复尝试。

正在进行静默恢复时再次请求交互登录，会复用该进程并允许它在需要时打开验证窗口，避免请求被静默模式吞掉。

不保存账号密码；会话文件和浏览器配置仅在本机 `.local/` 内保存，并由 `.gitignore` 排除。登录状态以超星服务器为准，按需更新，不人为延长 Cookie 有效期，也不在闲置时定时访问网站。

```powershell
python manage_login.py --status
python manage_login.py --refresh
python manage_login.py --refresh --silent
```

`--status` 查看最近一次恢复任务；`--refresh` 即使 HTTP 会话有效也刷新专用浏览器会话；`--silent` 不打开扫码窗口，无法静默恢复时返回 `login_required`。交互验证默认等待 10 分钟。自定义 `--auth` 文件可继续用于下载，但自动网页登录仅管理默认的 `.local/auth.json`。

## 查看指定日期的课程

```powershell
python download_date.py 2026-10-26 --list-only
```

## 下载指定日期的全部回放字幕

```powershell
python download_date.py 2026-10-26
```

替换日期即可查询其他日期。程序从课程实际时间戳计算周次，并与课表日期核对。同名课程的不同回放编号分别处理。尚未开始或正在直播的课程不尝试下载。

结果位于 `subtitles/日期/`：每个节次及课程目录下包含 VTT 和 TXT，`manifest.json` 记录逐项结果。重复执行会跳过已有有效 VTT；失败项重新尝试。使用 `--overwrite` 可以重新下载。

VTT 的文件头、时间轴和有效字幕片段使用同一解析器校验，损坏的缓存会重新下载；不会仅凭文件头或 `-->` 就判为成功。目标文件、临时文件及其上级目录的链接也会在写入前检查。

默认在本次日期的全部课程字幕成功导出、且 VTT/TXT 文件校验通过后，清理更早日期的程序生成目录及其中的字幕、清单。失败、无字幕、暂无回放或空课表时保留历史文件。更晚日期、未识别的目录、含用户额外文件的目录及链接目标不清理。早期单课脚本写在输出根目录的数字文件名样本，仅在确认与生成字幕内容重复时删除。清理详情记录在清单的 `cleanup` 字段中。

如果某次需要保留历史：

```powershell
python download_date.py 2026-10-26 --keep-history
```

所有按日期的导出共用输出锁，避免一个任务清理另一个仍在写入的目录。登录态、专用浏览器配置、项目代码和后台任务状态不参与字幕清理。

默认查询网页当前选中的学期。其他学期可指定 `--semester-id 编号`；编号来自课表学期下拉框的实际值。日期超出选定学期或接口结构无法识别时会报错，避免导出错误日期。

## 按单个编号下载

```powershell
python download_live.py 12828087
```

## 状态与检查

- `complete`：回放字幕已保存或已有有效文件。
- `unavailable`：当前课表没有可用回放入口。
- `no_subtitle`：字幕接口返回不含时间轴的空 VTT。
- `failed`：网络、权限、响应格式或文件保存失败。
- `pending`：尚未执行，例如任务被中断。

退出码：0 表示执行完成且无失败项；1 表示查询或整体执行失败；2 表示批次中存在失败项；3 表示字幕已导出但历史清理出错。空课表和无可用回放会明确显示数量。

```powershell
python -m unittest discover -s tests -v
```

离线测试使用系统临时目录与模拟 HTTP，不依赖真实登录态，也不把测试用的锁文件留在项目运行目录。

## 临时定位关键语句

```powershell
python search_subtitles.py "网络安全" --date 2026-10-26 --limit 10
```

不指定 `--date` 时，搜索最近一次导出的日期。返回课程、节次、原文、字幕起止时间、前后文、VTT/TXT 行号及回放链接。匹配忽略标点、空白和英文大小写，支持一句话跨多个字幕片段；当前采用文字匹配，不做语义改写匹配。

如果 TXT 被手工修改或已不存在，仍返回 VTT 原文、时间点和行号，但不返回未经验证的 TXT 行号；`text_mapping_warnings` 提示这些文件。个别字幕损坏时继续检索其他有效文件，并通过 `invalid_files` 返回损坏数量。

时间点精度为字幕片段级。回放链接打开对应页面后，可按显示的时间定位；尚未验证播放器的自动跳转参数。

搜索结果临时返回，不另建长期标注库或索引。对应日期的字幕删除后，本地不再保留其关键句追踪数据。清理正在进行时，搜索会等待短暂的输出锁，超时后可重试。

## 在 Codex 中使用 MCP

按 [安装说明](INSTALL.md) 将 `chaoxing-subtitles` 注册为 Codex 的本地 STDIO 服务。重启 Codex 后，可以直接说：

> 用 chaoxing-subtitles 检查超星登录态，然后下载 2026-10-26 的所有课程回放字幕。

服务提供七个工具。更新代码后重启 Codex，以加载新增的登录工具和会话恢复行为：

| 工具 | 用途 |
| --- | --- |
| `check_session()` | 检查并更新会话，失效时启动后台静默恢复 |
| `list_courses(date, semester_id?)` | 查询某天的课程和回放状态 |
| `start_export(date, semester_id?, overwrite?, keep_history?)` | 启动后台导出，全部成功后默认清理更早日期的结果 |
| `get_export_status(job_id)` | 查询进度、失败项和结果位置 |
| `search_subtitles(query, date?, limit?, context?)` | 临时定位关键语句的时间点、前后文和文件行号 |
| `start_login(force_refresh?, interactive?)` | 启动后台登录恢复，必要时打开扫码或验证窗口 |
| `get_login_status()` | 查询恢复进度、是否需要用户验证及完成状态 |

导出阶段只返回统计和文件位置，字幕正文保存在本地。同一日期已有任务执行时会返回现有任务。任务状态保存在 `.local/jobs/`；若后台进程被宿主结束，查询时会标记为中断，可重新发起导出并跳过已有有效文件。

多个登录请求会共用同一个恢复进程，避免重复打开浏览器。`check_session` 和只读课表查询不会长时间等待扫码；Agent 可以调用 `start_login` 后查询 `get_login_status`。后台导出会自行等待并恢复，其进度中的 `login` 字段会提示需要完成的验证。原有运行中的 MCP 进程需重启后才能使用新逻辑。

例如可以对 Agent 说：“下载 2026-10-29 的全部字幕，成功后清理旧日期的字幕。”或者：“在 2026-10-26 的字幕中定位‘网络安全’，给我时间点、前后文和回放链接。”每个后台任务独立保存统计，清单也带有精确的 `job_id`。同一天后续导出不会覆盖旧任务的进度和错误；结果被清理或已由后续任务替代时，旧任务的 `output_available` 为 false。

MCP STDIO 由客户端启动和管理，直接运行 `python server.py` 时等待输入且无普通终端输出是正常现象。可用以下命令独立验证协议连接：

```powershell
python scripts/check_mcp.py
```

需要真实查询课表并测试导出时：

```powershell
python scripts/check_mcp.py --date 2026-10-26
```

协议连接不调用模型 API，无需额外配置 API Key。Codex 的 MCP 配置方式见[官方文档](https://developers.openai.com/codex/mcp)。会话、原始诊断样本、任务状态及导出文件通过 `.gitignore` 排除。

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。
