# 持久网格网站入口

更新：2026-10-14。独立 Hyperliquid 账户已保存并通过接入检查，BTC做多/GTC普通网页创建及真实退出已验证。币种资格和已完成实盘证据见[普通网站实盘入口](/Users/alex/Work/entropy/planning/grid/GRID-LIVE-RELEASE.zh-CN.md)；其他币种验证仍在推进。

入口：**[网格账户](http://127.0.0.1:8789/#/grid/accounts)**。这是本机固定端口的独立网格网站，仅包含我的网格、创建网格和网格账户。旧 `51102` 临时入口已经停止；长期运行的旧套利控制台没有新网格路由。

## 已启动的持久服务

- `grid_main.py`：独立生产运行时，数据库为 `data/grid-runtime.sqlite3`，RPC 为 `data/run/grid-runtime.sock`，加密凭证目录为 `data/grid-credentials`。当前服务设置 `GRID_REAL_DISPATCH=1` 并加载权限600的私有币种资格文件；未获资格的币种和设置仍不能普通开单。默认启动不加载资格或不开总开关时禁止新增实盘风险。
- `grid_web.py`：固定监听 `127.0.0.1:8789`，只提供静态网格页面与网格 RPC 代理。不初始化旧 profiles、套利 .env、旧数据库、Supervisor 或 worker。
- 两个服务本次已在后台启动，状态记录在 `data/run/grid-local-services.json`。关闭页面不会清除配置；已设置macOS登录自启动和退出后自动恢复；网站、执行服务分别管理。详见[本机服务管理](/Users/alex/Work/entropy/planning/grid/GRID-SERVICES.zh-CN.md)。
- 网格网站验证客户端与 Host 都为本机，拒绝跨域请求和外部域名 Host，凭证接口仍执行原有同源保护。仅支持本机入口，没有开放远程访问。
- 不记录 HTTP 访问日志或账户请求正文；静态资源和接口响应不缓存。凭证仍由运行时加密保存，网站进程不打开凭证库。

## 使用步骤

1. 打开上述「网格账户」，确认显示「独立账户配置已就绪」，并且没有「临时验收环境」提示。
2. 选择主网或测试网，填写账户名称、资金账户地址，以及专用 API 代理钱包私钥。保存账户不需要币种。私钥仅填写页面，不发聊天。资金账户与代理钱包都由使用者保证与套利分开。
3. 点击「保存独立网格账户」，然后点击账户卡片上的「检查接入」。保存和检查不会下单；失败项按页面提示处理。
4. 进入「创建网格」，选择同一账户和本次要交易的永续合约，分别设置币种的区间、杠杆和投入金额。同账户可以管理多个不同币种的网格，资金预留合并核算；同一币种仅允许一个未结束网格。
5. 当前账户BTC支持做多、GTC、1–3倍全仓、立即/延迟建仓。预览通过后可在页面创建并启动实盘网格；ETH等其他币种须先完成相应真实循环证据。多币种实现与验证见[多币种账户报告](/Users/alex/Work/entropy/planning/grid/GRID-MULTI-MARKET.zh-CN.md)。

## 手动启动方式（当前已由系统管理，勿重复启动）

以下两个服务需要分别保持运行。已有服务运行时不要重复启动。先启动运行时，再启动网站。

```sh
GRID_REAL_DISPATCH=1 /Users/alex/Work/entropy/.venv/bin/python /Users/alex/Work/entropy/grid_main.py --mode production --db /Users/alex/Work/entropy/data/grid-runtime.sqlite3 --socket /Users/alex/Work/entropy/data/run/grid-runtime.sock --credentials-dir /Users/alex/Work/entropy/data/grid-credentials --grid-live-policy /Users/alex/Work/entropy/data/run/grid-live-release-policy.json
```

```sh
/Users/alex/Work/entropy/.venv/bin/python /Users/alex/Work/entropy/grid_web.py --port 8789 --socket /Users/alex/Work/entropy/data/run/grid-runtime.sock
```

凭证备份仍需同时保存 `master.key` 和 `accounts.enc`，运行数据库也需保留。不要将这些文件移动到静态目录或提交仓库。

## 最初恢复页面时的验证（2026-10-12）

- 真实浏览器已显示正常样式、API 在线、持久账户配置就绪及完整保存表单。
- 新增6项网站测试：真实 HTTP → RPC → 运行时的账户保存与关闭后恢复、旧套利路由不存在、跨域请求/空 Origin/域名伪装拒绝、令牌必须使用请求头以及运行时离线提示。只使用公开测试私钥和临时测试库，不访问交易所。
- 相关账户/API/网站测试：28 passed；全项目回归：770 passed、2 skipped、108 warnings（50.24秒）。新增入口 Python 编译、共享路由 JavaScript 语法和差异格式检查通过。
- 实际后台服务启动检查返回 production、temporary=false、real_dispatch=false。没有提交用户凭证，也没有发起交易所交易请求。
