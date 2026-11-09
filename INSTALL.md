# 超星字幕 MCP 安装说明

这个本地 MCP 用于导出西安电子科技大学超星录直播的课程回放字幕。请使用你自己的超星账号。账号需要能在浏览器里看到相应的回放和字幕。其他学校的录直播入口可能不同，当前版本未验证。

## 1. 解压并安装

准备 Windows、Microsoft Edge 和 Conda。将 ZIP 解压到一个可写的文件夹，并在包含 `server.py` 的目录打开 PowerShell。以下命令使用清华 PyPI 镜像安装 Python 依赖。

```powershell
conda create -n chaoxing-subtitles python=3.11 -y
conda activate chaoxing-subtitles
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
python scripts/check_mcp.py
```

如果已有同名 Conda 环境，直接激活即可。检查输出应包含 `MCP connected. Tools:`。这一步只验证 MCP 连接，无需登录超星。

## 2. 登录超星

```powershell
python manage_login.py
```

如果弹出专用 Edge 窗口，在里面用自己的账号扫码或完成验证。登录成功后，程序会在本机的 `.local/` 中保存会话供后续复用。以后如果收到需要重新验证的提示，再按提示操作即可。如果在校外无法访问回放，先在 Edge 中验证学校要求的网络访问方式。

## 3. 注册到 Codex

在同一个 PowerShell 窗口运行以下命令。需要已安装 Codex CLI 并且 `codex` 命令可用。

```powershell
$pythonPath = (python -c 'import sys; print(sys.executable)')
$serverPath = (Resolve-Path -LiteralPath '.\server.py').Path
codex mcp add chaoxing-subtitles --env PYTHONIOENCODING=utf-8 -- "$pythonPath" -X utf8 -u "$serverPath"
codex mcp get chaoxing-subtitles
```

如果网络确实需要代理，在注册命令的 `--` 前用 `--env HTTP_PROXY=实际代理地址` 和 `--env HTTPS_PROXY=实际代理地址` 指定你自己的设置。注册完成后重启 Codex。服务器由 Codex 启动，注册后可直接在聊天中调用。

如果使用其他支持本地 STDIO MCP 的客户端，将命令设为上面得到的 Python 绝对路径，参数依次填入 `-X`、`utf8`、`-u` 和 `server.py` 的绝对路径，环境变量填入 `PYTHONIOENCODING=utf-8`。

## 4. 先试查，再导出

先在 Codex 聊天中发送：

> 使用 chaoxing-subtitles MCP 检查我的登录状态，再列出 YYYY-MM-DD 的课程和回放。只查询课表。

把日期替换成自己有课的那一天。确认回放列表后，可以继续发送：

> 使用 chaoxing-subtitles MCP 导出 YYYY-MM-DD 的全部回放字幕，保留历史字幕，并告诉我保存位置。

字幕保存在项目的 `subtitles/日期/` 目录。导出工具默认会在当天全部字幕导出并通过校验后，清理更早日期的程序生成文件。如果想长期保留旧字幕，请在指令中明确要求保留历史（`keep_history=true`）。需要定位关键句时，可以让 Codex 在某天字幕里查词并给出回放时间点。

重新打包分享时，只包含源码和说明。登录后生成的 `.local/`、字幕目录 `subtitles/` 及本机代理配置应留在本机。
