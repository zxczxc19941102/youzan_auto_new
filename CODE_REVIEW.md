# 代码评审说明

本分支用于对 `main` 当前版本进行集中评审，不包含账号、密码、Cookie、Token、短信验证码、运行日志、截图或本地构建产物。

## 建议评审范围

- 登录流程：账号密码真实校验、短信验证码、工作台确认。
- 本地配置：Windows DPAPI 密码保护、Cookie/auth_state 的 Git 忽略规则。
- 浏览器自动化：Playwright 页面操作、错误处理和停止机制。
- 店铺跟进：单线程策略、服务端限流提示、失败记录和重复填写确认。
- 构建发布：`.venv` 路径适配、PyInstaller、UPX、本机自签名限制。
- 测试：离线回归测试、编译检查和依赖检查。

## 本地验证命令

```powershell
Get-ChildItem .\tests -Filter *.py | ForEach-Object {
    .\.venv\Scripts\python.exe $_.FullName
}
.\.venv\Scripts\python.exe -m compileall -q .\main.py .\py .\tests
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

## 敏感信息要求

评审、提交和 PR 中不得粘贴真实账号密码、Cookie、Token、短信验证码、运行日志或包含客户数据的页面截图。
