# 有赞后台自动登录（滑块 + 短信 + 抓CK）

后台 headless Edge 自动登录有赞渠道后台，自动破解滑块，短信验证码由人工填写，登录成功后保存 Cookie/Token 供下次免登录复用，并把登录后的页面源码保存为 HTML。

## 目录结构

```
youzan_auto/
├─ main.py              入口主程序（GUI + --smoke 冒烟）—— 固定在根目录，不归档
├─ requirements.txt     依赖清单
├─ README.md
├─ py/                  核心 Python 模块
│  ├─ config.py         配置（URL / 路径 / 账号 / 浏览器）
│  ├─ captcha.py        滑块：缺口检测 loc_gap + 模拟真人拖动 human_drag
│  ├─ youzan_bot.py     YouzanBot：登录→滑块→短信→抓CK/页面源码/复用
│  ├─ shop_followup.py  指定店铺跟进记录自动化测试用例（列表→详情→接口→表单→发布）
│  └─ task_runtime.py   GUI/任务运行时、线程池与日志运行时支持
├─ json/                登录凭据（自动生成，敏感，勿外传）
│  ├─ cookies.json      Cookie/CK
│  └─ auth_state.json   Cookie + localStorage TOKEN（优先使用）
├─ html/                登录成功后的页面源码（带时间戳）
├─ txt/                 诊断/报告/短信验证码中转文件
├─ png/                 调试截图
└─ venv/                虚拟环境
```

> `json/`、`html/`、`txt/`、`png/` 由程序自动创建；这些是运行产物，已在 `.gitignore` 中排除。

## 运行

`main.py` 启动时会自动切换到项目自带的 `venv` 解释器，并把 `py/` 加入模块搜索路径，所以在哪个目录用哪个 python 跑都可以：

```powershell
cd e:\Codebuddysoft\有赞客户跟进\youzan_auto
python main.py            # 图形界面
python main.py --smoke    # 无 GUI 冒烟，验证能跑到短信阶段

# 也可显式指定 venv 解释器：
.\venv\Scripts\python.exe main.py
```

账号密码通过环境变量提供，不写入源码：

```powershell
$env:YOUZAN_USER = '你的账号'
$env:YOUZAN_PASSWORD = '你的密码'
python main.py
```

## 使用流程

1. 运行 `main.py`，界面出现。
2. 确认/修改「账号、密码」，点「开始登录」。
3. 日志显示「滑块 通过 OK」后，按提示完成短信验证码。
4. 登录成功（免密 / 密码直达 / 短信验证码）后：
   - 统一跳转到工作台 `https://qudao.youzan.com/#/workbench`（即截图中的页面）；
   - 更新 `json/cookies.json`（Cookie/CK）和 `json/auth_state.json`（含 localStorage TOKEN）；
   - 抓取当前页面完整源码，保存为 `html/<登录方式>_YYYYmmdd_HHMMSS_mmm.html`，例如：
     `html/免密登录_20261022_120909_123.html`、`html/短信登录_20261022_121530_456.html`；
   - 每次登录生成新文件，不会覆盖历史记录。免密失败时另存 `html/免密登录_失败_*.html` 便于排查。
5. 会话冲突处理：若页面出现「您的账号已在其他PC登录，请重新登录」，程序不会把它当成功，
   而是点击「重新登录」结束旧会话并重新走登录流程；新登录会顶掉其他设备，结束后落到工作台。
6. 下次启动优先用完整登录态免密进入，成功后继续刷新本地凭据和 HTML。

## 填写第一页前 3 条跟进记录

```powershell
cd e:\Codebuddysoft\有赞客户跟进\youzan_auto
python main.py --fill-first3-dry   # 只填表，不发布
python main.py --fill-first3       # 真实填写并发布前3条
```

- 读取店铺列表第一页，按顺序取前 3 家。
- 逐店点击「详情」，在新标签里打开店铺详情。
- 点击「拜访商家」，填写：电话拜访 / 了解需求 / 沟通情况第一个 / 有需要联系我。
- 点击「发布」，通过 `POST /visit/save HTTP=200` 和页面结果确认成功。
- 控制台只输出店铺块，不输出过程中的页面/接口日志。
- 成功状态「跟进记录：填写完成」在 GUI/HTML 中为**红色加粗**；失败为绿色。
- 最后输出：`填写 X 条记录已完成，填写成功 X 条，失败 X 条。`
- 批量结果保存到 `txt/followup_batch_*.html` 和 `.json`。

## 全量跟进（支持 1~10 线程，默认 3）

GUI：
- 勾选「登录后统计店铺并填写全部跟进记录」。
- 「全量跟进试运行（只填表不发布）」默认不勾选；勾选后只填表不发布。
- 「并发线程」下拉选择 `1~10`，默认 `3`（低并发更稳，可减少「操作太快」风控提示）。
- 登录成功后按选择线程执行；每个线程使用独立 Playwright/Edge，共享已登录的 `auth_state.json`。
- 单店失败不中断整批，记录失败后继续下一家，最后汇总成功/失败/终止数量。
- 运行日志实时写入 `LOG/run_*.txt`，其中包含每个步骤的开始/结束明细与耗时。
- 结果同步保存到 `txt/followup_batch_*.txt/.json/.html`。

CLI：

```powershell
python main.py --fill-all-dry --workers 10   # 10线程试填写，不发布
python main.py --fill-all --workers 5        # 5线程真实发布
python main.py --fill-all --skip 3 --workers 5  # 跳过前3家已完成的
```

并发实测（只填表不发布）：

- 2 线程：6/6 成功
- 5 线程：5/5 成功，49.4s
- 8 线程：6/8 成功，90.3s
- 10 线程：5/12 成功，主要失败为 `Page.goto` 30 秒超时、慢加载找不到拜访表单

结论：5 线程稳定；8 线程开始不稳定；10 线程明显过高。建议正式使用 2-5 线程，8/10/15/20 仅用于观察，不建议发布。

## Git 推送（自动判断代理）

`push-git.ps1` / `push-git.cmd` 会先检测本机 `127.0.0.1:10808`：

- 检测到代理：使用 `socks5h://127.0.0.1:10808` 推送；
- 未检测到代理：清空代理配置，直接连接 GitHub。

用法：

```powershell
cd E:\Codebuddysoft\有赞客户跟进
.\youzan_auto\push-git.cmd
```

等价于默认执行 `git push -u origin main`。也可以传完整 Git 参数：

```powershell
.\youzan_auto\push-git.cmd push -u origin main
.\youzan_auto\push-git.cmd -ShowMode ls-remote origin
```

这个脚本只在单次命令上用 `git -c` 指定/清空代理，**不会修改全局 Git 配置**。

注意：
- 关代理时脚本会走直连；如果当前网络本身无法直连 GitHub（本机测试为 `Test-NetConnection github.com:443 = False`），关代理仍然推不上去，需要可用网络或其他代理。
- IDE 的「发布 Branch」按钮不会自动调用这个脚本；按钮推送需要手动设置 Git 代理，或用本脚本/终端推送。

## 读取我的店铺列表（只读）

```powershell
cd e:\Codebuddysoft\有赞客户跟进\youzan_auto
python main.py --list-shops
```

- 打开 `https://qudao.youzan.com/index#/shop/list/my`。
- 自动翻页读取全部店铺；每个店铺点击「详情」，捕获新标签的实际 URL 后关闭标签。
- 每个店铺按以下格式输出日志，**控制台只输出这些块**：
  ```
  ----------------------------------------------
  店铺名称：班会智库
  店铺ID：40785558
  跟进网址：https://qudao.youzan.com/#/shop/detail/2tUkp1ko7b6G19m5AZFF9fwjh
  ----------------------------------------------
  ```
- 店铺之间只保留一条分割线，没有时间戳、接口行、页码行。
- 登录/接口/分页/异常等运行细节写入 `txt/shop_list_run_*.log`，不影响结果输出。
- 同时保存 `txt/shop_list_*.json`、`.txt`、`.csv`（含详情 URL）。
- 只读操作，不修改任何数据。
- GUI 默认已勾选「登录后读取店铺列表并输出日志」，且自动发布默认关闭，登录后会直接读取列表。

## 指定店铺跟进记录测试用例

GUI 中「登录成功后自动填写并发布跟进记录」默认关闭，避免每次登录重复新增记录；
需要发布时手动勾选。当前阶段默认勾选的是「登录后读取店铺列表并输出日志」（只读）。

入口固定为 `main.py`：

```powershell
cd e:\Codebuddysoft\有赞客户跟进\youzan_auto
python main.py --test-followup-dry   # 只填表+截图，不点击发布（安全预演）
python main.py --test-followup       # 真实点击发布，会新增一条跟进记录
```

流程：
1. 登录成功后直接打开 `https://qudao.youzan.com/#/shop/detail/2tUkp1ko7b6G19m5AZFF9fwjh`。
2. 抓取详情页右侧区域、标签页、链接和功能点，保存到 `txt/shop_detail_dump_*.json/.txt`。
3. 点击右侧「拜访商家」展开拜访面板（面板默认可能是收起状态）。
4. 按测试数据填写：
   - 跟进方式：电话拜访
   - 跟进目的：了解需求
   - 沟通情况：第一个选项（电话打通，有交流）
   - 客户需求/备注：有需要联系我
   - 联系人、跟进日期使用页面默认值
5. 下拉/日期浮层出现「确定」会自动点击；随后点击「发布」，若再弹确认框自动确认。
6. 通过页面提示或发布接口返回（`code=0/success=true`）确认结果，输出报告。

输出文件（均在 `txt/`、`png/`、`html/` 下）：
- `followup_test_result_*.json/.txt`：步骤、表单值、接口清单、成功/失败原因
- `api_capture_*.json`：完整接口录制（URL 已脱敏，响应正文保存在本地）
- `shop_detail_dump_*.json/.txt`：详情页右侧链接与功能点
- `followup_*.png/.html`：各步骤截图和页面源码

登录说明：用例会先复用有效登录态；若需要短信验证码，设置 `YOUZAN_SMS_CODE` 环境变量，或在提示后写入 `txt/sms_code.txt`。这是唯一需要外部输入的一步，其余流程自动执行。

## 依赖 / 配置

- 依赖：`playwright / pillow / numpy`（`venv\Scripts\pip install -r requirements.txt`）
- 浏览器：复用系统 Edge（`config.BROWSER_CHANNEL='msedge'`），无需额外下载 chromium。
- 配置：改 `py/config.py`（URL、HEADLESS、默认账号、各归档目录等）。

## 重要提示（坑点详见项目记忆）

- 滑块轨迹是页面 JS 加密后提交的 `userBehaviorData`，不可伪造/重放；本项目用「模拟真人拖动」让页面自己加密提交。
- 缺口定位必须用「拼图剪影轮廓 vs 背景梯度」的形状边缘相关，不能用颜色/方差/内容模板匹配。
- 取拼图必须读「当前可见容器」里的图（页面有隐藏模板节点），否则会拿到错误 token 的图。
- Windows 控制台 GBK 会因 emoji/特殊字符崩溃，代码已强制 stdout/stderr 为 UTF-8。
- 保存的 HTML 可能包含账号/客户等页面数据，仅保存在本地 `html/` 目录，不要随代码提交或外传。

## 快速开始（Windows）

项目提供了初始化脚本和启动脚本，首次运行建议按以下步骤操作：

```powershell
cd F:\youzan_auto
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
.\run.cmd
```

`setup.ps1` 会自动完成以下工作：

1. 创建项目虚拟环境 `venv`；
2. 安装 `requirements.txt` 中的依赖；
3. 检查核心模块是否能够导入；
4. 检查系统 Microsoft Edge 是否存在。

后续启动可以直接使用：

```powershell
.\run.cmd                         # 启动图形界面
.\run.cmd --smoke                 # 无头冒烟检查
.\run.cmd --list-shops             # 只读读取店铺列表
.\run.cmd --fill-first3-dry        # 前 3 家安全试运行，不发布
.\run.cmd --fill-all-dry           # 全量试运行，不发布
.\run.cmd --fill-all --workers 3   # 全量真实发布，3 线程
```

也可以显式调用虚拟环境解释器：

```powershell
.\venv\Scripts\python.exe .\main.py
```

### 运行前检查

- 操作系统：Windows；
- Python：建议 3.10 及以上；Python 3.14 已兼容；
- 浏览器：Microsoft Edge；
- 网络：能够访问有赞登录页和业务接口；
- 首次登录：准备好账号、密码，以及可能需要的短信验证码。

## 本地配置与敏感数据

登录成功后，程序会在项目本地生成以下文件：

```text
json/cookies.json       登录 Cookie/CK
json/auth_state.json    Playwright 完整登录态
json/local_config.json  账号、加密密码、复选框状态、并发数
```

`json/local_config.json` 中的密码使用 Windows 当前用户 DPAPI 加密保存，不保存明文密码。程序下次启动时会自动恢复：

- 账号和密码；
- 登录后读取店铺列表选项；
- 自动填写全部跟进记录选项；
- 全量跟进试运行选项；
- 并发线程数。

密码输入框使用掩码显示，日志只输出 `密码=***`，不会打印密码内容。上述登录态和本地配置均已加入 `.gitignore`，不会被 Git 跟踪；不要手动将 `json/` 目录中的敏感文件上传或发送给他人。

如果需要使用环境变量覆盖本地配置：

```powershell
$env:YOUZAN_USER = '你的账号'
$env:YOUZAN_PASSWORD = '你的密码'
.\run.cmd
```

环境变量优先级高于 `json/local_config.json`。

## GUI 选项说明

- **登录后读取店铺列表并输出日志**：只读获取店铺，不修改数据；默认开启。
- **登录后统计店铺并填写全部跟进记录**：登录成功后执行批量跟进；请确认业务数据后再开启。
- **全量跟进试运行（只填表不发布）**：用于验证页面定位和表单流程，不点击发布；建议首次使用先开启。
- **并发线程**：可选 1~10，默认 3。并发越高速度越快，但更容易触发页面限速或风控，建议从 1~3 开始。

## 故障排查

### `AttributeError: ... _initializer`

这是旧版线程池代码与 Python 3.14 内部 API 变化导致的错误。当前版本已在 `py/task_runtime.py` 中兼容 Python 3.14；请重新执行 `setup.ps1` 后再启动。

### `libpng warning: iCCP: known incorrect sRGB profile`

这是验证码图片自带 ICC 色彩配置产生的 libpng 警告，不是登录失败原因。只要后续流程继续运行，通常可以忽略。

### 找不到 Edge 或 Playwright 启动失败

确认 Microsoft Edge 已安装并可正常打开。如果依赖损坏，执行：

```powershell
.\venv\Scripts\python.exe -m pip install --upgrade -r .\requirements.txt
```

项目默认复用系统 Edge，不需要额外下载 Chromium。

### 短信验证码没有自动提交

在 GUI 中输入 6 位数字验证码后点击「提交验证码」。命令行测试也可以通过环境变量或本地文件提供验证码：

```powershell
$env:YOUZAN_SMS_CODE = '123456'
```

### 登录态失效

删除本地 `json/cookies.json` 和 `json/auth_state.json` 后重新登录。不要删除 `json/local_config.json`，否则账号、密码和界面设置也会被清除。

## 开发自检

提交代码前可以执行：

```powershell
$files = @('.\main.py') + (Get-ChildItem .\py -Filter *.py | ForEach-Object FullName) + (Get-ChildItem .\tests -Filter *.py | ForEach-Object FullName)
.\venv\Scripts\python.exe -m py_compile $files
.\venv\Scripts\python.exe .\tests\test_verify_false_negative.py
.\venv\Scripts\python.exe .\tests\test_publish_wait.py
git diff --check
```
