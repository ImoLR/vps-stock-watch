# vps-stock-watch

一个面向多 VPS/IDC 商家的轻量监控器。Provider 只负责把站点转换成统一
`Product`；完整目录比较、JSON 状态、失败退避、Telegram 和 `/status` 都在核心层。

内置 NexKr、DMIT、Blossom Host、BOILCLOUD、FACHOST、LeiKwanHost、利群汇聚、VMSILO 与 Misaka，
同时提供 CSS、XPath、Regex、JSONPath 规则，可用 YAML 接入结构简单的网站；复杂网站继续
增加 Python Provider。

## 已核实的数据源（更新至 2026-09-29）

### NexKr

实际检查了 [商城](https://nexkr.sh/app/shop)、入口 HTML、当前 Vite JavaScript bundle
以及浏览器所调用的接口。商城 HTML 只有 SPA 容器；`Shop` bundle 直接执行
`GET /api/v1/shop/groups`，所以主数据源采用公开 JSON：

`https://nexkr.sh/api/v1/shop/groups`

该接口当前返回：

- 分组：`id`、`slug`、`title`、`type`、区域和子分组；
- 商品：`id`、`slug`、`name`、`group_id`、`subgroup_id`、`zones`；
- `price_usd`、`price_cents`、`setup_fee_cents`、`cycle_months`；
- `in_stock`（只有布尔库存）、`limited`、说明文字；
- schema 支持 `cores`、`memory_mb`、`disk_gb`、`bandwidth_gb`，当前商品未必都提供。

当前接口不提供具体库存数量，因此 Provider 保留 `stock: null`，绝不把
`in_stock: false` 伪造成库存 0。商品规格优先读结构化字段；缺失时仅从说明中提取
明确写出的 CPU、RAM、磁盘、流量、带宽、IPv4/IPv6 文本。

完整 API 响应就是前端可访问的商品目录，新 ID 会自动进入基线比较。当前前端 bundle
没有发现第二个公开商品索引、GraphQL 或隐藏商品接口，因此无法声称能看到服务器从未
返回的草稿；但 API 返回的 TEST/Beta/Internal/测试商品不会被过滤。

### DMIT

DMIT 当前仍是 WHMCS 商城。目录主源是：

`https://www.dmit.io/cart.php`

服务端 HTML 中每个 `.cart-products-box` 自带 `pid`，外层卡片带 `gid`；卡片包含
名称、美元价格、付款周期、CPU、RAM、存储、线路、IPv4/IPv6、流量与带宽。
`.none-stock` class 或 `Out of Stock` 文本表示无货；没有这两个信号的卡片表示可下单。
页面没有公开具体库存数字，所以同样使用 `stock: null`。

真实目录能一次发现多个 PID，而不是写死 PID 183。商品购买链接统一为
`cart.php?a=add&pid=<PID>`。对 `extra_pids` 中的少量已知/隐藏 PID，Provider 还会检查
下单页：`Out of Stock` 表示无货；`Product Configuration` 或 `Order Summary` 表示有货。
默认不做无限 PID 枚举，避免对商家产生大量无意义请求。

本机的普通 HTTP 请求当前会收到 Cloudflare 403，因此 `fetch_mode: auto` 先尝试
HTTP，遇到 403/429/挑战页才启动 Playwright。2026-09-07 的真实验证表明 DMIT 同时
拦截 Chromium headless 标识，但 Xvfb 中的 headed Chromium 能取得 HTTP 200 和完整目录；
因此示例配置使用 `browser_headless: false`，运行命令通过 `xvfb-run` 提供虚拟显示器。
若部署 IP 可直接取得正常 HTML，就完全不使用浏览器。2026-10-02 再次抓取页面网络记录：
92 个商品卡片都来自服务端渲染的主 document，额外 XHR 只有 Cloudflare challenge/RUM，
前端 Ajax 只处理配置价格和账户功能；仍未发现比目录 HTML 更稳定的公开 JSON/REST/GraphQL
库存接口。selector timeout 会做一次有界重试，其他错误留给调度器按类型记录和退避。

### Blossom Host

真实检查了首页、`app.js`、浏览器打开 `#/buy/isp-xs` 时的 Fetch/XHR，以及购买页使用的
公开同源 JSON：

`https://blossomhost.us/api/catalog`

浏览器实际以 `GET /api/catalog` 获取购买目录；不需要 HTML selector 或 Playwright。
`plans` 是完整商品列表，每个对象提供稳定 `key`、名称、family、`offer_key`、location、
carrier、价格、规格、`in_stock`、`stock_count` 和 `availability_status`。Provider 使用
真实 `key` 作为 `product_id`，不写死 ISP 套餐，也不按显示名称合并。

Washington, DC 和 Seattle 是两个独立 `offer_key`，同名套餐也拥有不同 key。例如
Washington 的 `ISP-XS` 是 `isp-xs`，Seattle 的 `ISP-XS` 是 `isp-seattle-xs`，因此两地
分别建立库存对象。接口公开具体整数 `stock_count`，可检测任意库存数字变化，而不是只
检测有货/无货。

2026-09-14 的真实验证取得 42 个 plan：12 个 ISP（Washington 6、Seattle 6）和 30 个
裸金属商品；另有 1 个 `residential_inventory` beta offer。Provider 不过滤这个 beta
对象，因此共监控 43 个库存对象，其中 30 个有货。两个 ISP-XS 当时均为无货，
`stock_count` 均为 0。以上是点时快照，会随实时目录和库存变化。

Blossom 的 Telegram policy 使用 catalog 的机器分类 `plans[].family`，而不是商品名称或
SKU。`isp`（ISP Line）和 `metal`（Bare metal）仍完整抓取、比较并写入 state，也继续
计入 `/status`，但所有变化都标记为静默，不发送 Telegram；其他 family（包括独立的
residential inventory）保持通知。需要恢复某一系列时，只需调整
`BlossomProvider.SILENT_NOTIFICATION_FAMILIES`。

### BOILCLOUD

BOILCLOUD 当前使用 WHMCS 服务端商城 HTML，不使用 Playwright。普通 HTTP 请求就能在
`div.tt-single-product[id^="product"]` 商品卡片中取得真实 PID、名称、价格、付款周期、
整数库存、规格和商品链接。浏览器 Network 验证只看到语言包 JSON 和统计请求，没有
商品 catalog XHR；`/api` 是受 IP 限制的 WHMCS 管理 API，不是公开 catalog API。

每轮从首页导航、WHMCS 分类侧栏和 `sitemap.xml` 三路自动发现 `/store/<slug>`，取并集后
顺序遍历，并继续从每个分类页发现新分类。商品以卡片 `id="product<PID>"` 中的真实 PID
为唯一 ID，跨分类重复 PID 会合并分类元数据，不按名称或页面顺序去重。Telegram 商品
链接使用稳定的 `cart.php?a=add&pid=<PID>`。

2026-09-20 的真实完整扫描自动发现 11 个公开分类路由，其中 1 个旧路由重定向到 NAT，
得到 10 个唯一分类页面、52 个唯一 PID。10 个有货、42 个无货，52 个商品全部取得整数
库存；6 个商品来自仍在站点导航中、但不在当前 WHMCS 分类侧栏和 sitemap 中的 3 个旧
分类。扫描使用同一 HTTP Session、13 个顺序请求，约 15 秒完成；点时结果会随商城变化。

首页、商城入口、sitemap 或任何已发现分类只要发生 HTTP、超时或解析失败，整轮就失败；
旧 BOILCLOUD state 保持不动，因此不会把分类临时失败误报为大量商品下架。只有所有分类
都完整成功后才会提交 snapshot 并允许 `removed_product`。默认间隔为 120 秒，运行时仍可
通过 Telegram 输入任意 10–86400 范围内的整数秒数并原样持久化。

### FACHOST

FACHOST 官方主站和公开商城位于 `https://fachost.cloud/`，使用 Paymenter + Livewire，
不是 WHMCS。公开商品分类使用 `https://fachost.cloud/products/<category-slug>`，购物车为
`https://fachost.cloud/cart`，可购买商品的直接结账路径为
`/products/<category-slug>/<product-slug>/checkout`。

站点的 `/api/v1/admin/products` 和 `/api/v1/admin/categories` 是需要认证的管理 API；没有
发现可用的公开 catalog REST/GraphQL 接口。商品分类页本身已经服务端渲染完整目录，
因此 Provider 直接使用普通 HTTP HTML，不需要 Chromium，也不调用登录后的 Dashboard。

每轮先从官网导航自动发现 `/products/<category-slug>`，再遍历分类页，并继续从每个分类
页面发现新增分类。分类页 `wire:name="products"` 的 Livewire snapshot 提供真实 Paymenter
`Product` 数据库 ID 和 `Category` ID；Provider 将真实 Product ID 作为稳定唯一键，再按
同一顺序与服务端商品卡严格配对。ID 数量、商品卡数量、分类导航或 snapshot 结构不一致
都会使整轮失败，旧 state 保持不动，不会误报批量下架。

库存卡片显示 `N available` 时保存具体整数；若页面只给 `Available` 或 `Sold out`，则仅
保存布尔状态，不猜测库存数字。名称、价格、付款周期、CPU、内存、磁盘、带宽、流量、
IPv4/IPv6、分类、地区、详情页与直接结账页都从公开 HTML 取得。首次成功只建立 FACHOST
baseline；默认扫描间隔为 60 秒，可从 Telegram 的独立 Provider 菜单修改并持久化。

2026-09-27 的真实完整扫描从当前商城导航自动发现 5 个公开分类和 16 个可见商品；另有
搜索索引仍能发现、当前可直接公开结账但不在商城导航中的 Hinet-VDS-Lite（真实 Product
ID 22）。该 checkout 作为可维护的 `extra_product_urls` 补充入口监控，404 时按商品真正
消失处理，其他请求或解析错误仍使整轮失败。合计 17 个唯一 Product ID：1 个可下单、
16 个售罄，均未公开具体库存数字。完整扫描使用 7 个普通 HTTP GET（首页、5 个分类页和
1 个补充 checkout），约 4–5 秒完成，无重复 Product ID、无公开测试商品，也无需
Chromium。这些数量是点时快照。

### LeiKwanHost

LeiKwanHost 的 `buy.leikwanhost.com` 是 WHMCS 服务端渲染商城。公开站点没有可用的
catalog JSON/API；`cart.php` 会跳转到默认商品组，每个分类页的侧栏和分类下拉框都提供
完整公开分类导航，商品卡直接提供真实 `product<PID>`、价格、付款周期以及精确
`N Available` 或布尔售罄/可订购状态。Provider 通过普通 HTTP 自动遍历全部分类并按 PID
去重，不需要 Chromium。

每次完整扫描会校验所有分类页仍包含入口页发现的分类导航，并设置生产环境分类数/商品数
安全下限；任一分类失败、导航残缺、布局异常或总量明显异常都会让整轮失败，旧 state 保持
不变。正常目录商品统一保存 `metadata.discovery.hidden=false`；当前 sitemap/robots 均不存在，
搜索索引只发现一个已 302 到当前 `hiternet` 的旧分类别名，没有发现额外可购买的隐藏商品。
商城扫描和商品购买链接固定使用官网的 CNY 货币编号 `2`，链接格式为
`cart.php?a=add&pid=<PID>&currency=2`，不会跟随新会话回落到默认 HKD。首次成功只建立静默 baseline，
默认扫描间隔为 120 秒。

### 利群汇聚

利群汇聚位于 `https://v3.leikwanhost.com/`，是独立的 LeiKwan Bridge PHP 门户，不是
`buy.leikwanhost.com` 的 WHMCS 商城。Provider 使用独立的 `liqunhuiju` namespace，绝不
与 LeiKwanHost PID 合并。公开首页当前可正常访问，具有稳定的
`body.pg-public.bridge-home-page`、`.public-shell`、登录与知识库入口；但没有公开商城链接或
商品容器，`robots.txt`、`sitemap.xml`、`store` 和 `cart.php` 当前均不存在。

因此当前首页被确认是完整可识别、但没有公开商品的合法空目录：首次扫描会建立
`products=0` 的静默 baseline，之后仍按 60 秒默认间隔持续检查。HTTP 200 但品牌、页面结构
或必要入口缺失时会判为 incomplete catalog。若旧 state 已有商品，而新页面只剩无目录的
landing page，也会拒绝空快照并保留旧 state；只有明确的完整 catalog 空标记才允许从
N 个商品变成 0 并产生正常下架变化。当前扫描只需 1 个普通 HTTP GET，不需要 Chromium。

### VMSILO

VMSILO 位于 `https://portal.vmsilo.com/`，已确认使用 WHMCS + ShufyTheme。没有发现公开
catalog JSON/API；分类、商品、价格、付款周期、配置和下单链接均已包含在服务端 HTML，
因此 Provider 使用普通 HTTP 自动发现首页及分类侧栏中的 `/store/<category>`，不启动
Chromium，也不调用购物车 AJAX。

商品卡没有公开 WHMCS 数字 PID，购物车中的 `i=0/1/...` 又是会话内索引，不能作为 ID；
Provider 因此使用公开商品 URL 中稳定的 product slug，并以 `vmsilo:<slug>` 保存。购买链接
保留网站给出的 `/store/<category>/<product-slug>`，不猜测 PID URL。若页面公开精确
`N Available` 就保存整数，否则只依据可订购/售罄按钮保存布尔状态，绝不伪造库存 1。

2026-09-27 的真实扫描发现 2 个侧栏分类：IEPL 有 6 个商品，socks5 以模板计数明确返回
0 个商品；6 个商品当前全部可订购，均只提供布尔库存，没有具体库存数字。首页旧 `cn2`
入口会重定向到 IEPL 并被去重；robots、sitemap 与搜索结果没有发现额外可购买的隐藏商品。
完整扫描使用 4 个普通 HTTP GET，默认间隔为 120 秒。分类侧栏缺失、模板商品计数与实际
卡片数不一致、请求失败或未知空布局都会拒绝整轮 snapshot，避免误报批量下架。

### Misaka

Misaka 的控制台是 Vite SPA；前端 API client 公开调用
`GET /api/mc2/regions` 和 `GET /api/mc2/regions/<region-slug>/plans`。Provider 直接使用这两个
JSON 接口，不抓取渲染后的 UI，也不需要 Chromium。地区使用 API 的 `slug`、`id` 和
`country_code`，套餐使用真实数字 `id`，最终唯一键为
`misaka:<region-slug>:<plan-id>`，不会把不同地区的同名 Size 合并。

监控分成两层：所有公开地区和 Plan 都进入 Global Catalog baseline，用于发现新地区和
全球新品；只有配置中的 `focus_regions: [HK, TW, JP]` 保存并比较完整库存、三种付款周期
价格、可选促销字段和下架。非重点地区仍保留轻量 catalog Product，但使用
`catalog_only=true` 和 `available=null`，所以不会产生补货、售罄、库存、价格、折扣或下架
通知，也不会出现在 `/stock`。港台日的 `available=false` Plan 绝不丢弃。

2026-09-29 实测为 42 个地区、361 个地区 Plan；港台日共 50 个 Plan，其中 37 个有货、
13 个无货。API 当前只提供 `available` 与 `unavailable_reason=out_of_stock`，没有具体库存
数字，因此 `stock=null`。价格字段为 `price_monthly`、`price_semiannual` 和
`price_annual`，币种为 USD；当前 schema 没有原价、折扣或 promotion 字段，也没有正在
进行的促销。完整扫描 43 个普通 GET，约 2.6–2.8 秒，默认间隔为 60 秒。

Misaka Route Watch 与库存扫描完全独立，默认每 1800 秒使用
[Misaka 官方 Looking Glass](https://misaka.ping.sx/) 的公开 WebSocket MTR，从香港、台湾、
日本各自最高的已标注线路等级（当前均为 Premium Plus）主动测向广东电信、联通、移动
三个固定 IPv4 目标。节点来自官方 probe directory，并与 `/api/mc2/regions` 的 speedtest
地址交叉匹配；IPv6 只发现并保存，第一版不测量、不通知。线路状态单独原子写入
`data/route_state.json`，不会与库存 state 互相覆盖。

线路比较使用 Team Cymru DNS 查询得到的 ASN、组织和 prefix，并缓存结果；fingerprint 只取
去重后的 ASN 序列，因此单个超时、接口 IP、同 ASN 内 hop 数或 RTT 抖动不会触发通知。
首次成功只建立静默 baseline；新路径必须连续两轮相同才确认并通知，单次 candidate 恢复
baseline 会被丢弃。LG、目标可达性、schema 或 ASN 覆盖率失败时保留旧 baseline，并把已取得
的 raw attempt 仅作为诊断状态保存。Route scheduler 自带防重入和失败退避，不阻塞库存线程。
`/status` 显示 Route Watch 健康信息，并提供“Misaka 当前线路”只读按钮；点击只读 state，
不会立即发起 traceroute。

参考结构：[NOAFF Restock Monitor](https://github.com/cshaizhihao/noaff-restock-monitor)
的通用规则、WHMCS 和商品去重思想，以及
[近期 DMIT 页面解析样例](https://github.com/Ra2fSt/dmit-monitor)。本项目代码为独立实现。

### 真实联网验证（2026-09-07，点时结果）

- NexKr API 用时约 0.56 秒，返回 3 个唯一商品 ID（1、22、23），均为有货，均不提供
  库存数字。
- DMIT 普通 HTTP 与 headless Chromium 均收到 Cloudflare 403；Xvfb 中的 headed
  Chromium 返回 HTTP 200，取得约 1.61 MB HTML 和 81 个商品卡片。
- DMIT Provider 实际解析 81 个唯一 PID：18 个有货、63 个无货、0 个提供库存数字；
  地区分布为 Los Angeles 48、Hong Kong 18、Tokyo 15。
- 下单页交叉检查 PID 253 得到 `Out of Stock`，PID 265 进入可配置流程，两者都与目录
  卡片状态一致。
- 使用真实 `config.yaml` 跑完整双 Provider 首次基线成功：NexKr 3、DMIT 81，两个
  Provider 均无错误，且首次基线产生 0 个变化和 0 个通知。

这些数量和状态会随商家实时库存变化；它们是验证时快照，不是写死在代码里的期望值。

## 工作方式

每个成功抓取都必须是完整目录；只有 Provider 能以明确 schema 证明的合法空目录才允许
返回 0 商品。首次成功只建立基线，不通知已有商品。之后以
`provider + product_id` 为唯一键比较：

- 新 ID：新品；名称含测试关键词时附加“疑似测试商品”，但不丢弃；
- `available false → true`：补货；`true → false`：售罄；
- 两次都提供数字且数值变化：库存变化；
- 价格、付款周期或名称变化：相应通知；
- 上一份完整目录中存在、当前完整目录消失：商品下架。

HTTP/解析失败只更新 Provider 错误和退避计数，不覆盖旧商品快照，也不会制造“全部
下架”。连续失败从 Provider 的 `failure_retry_seconds`（未配置时使用正常间隔）开始指数
退避，最高由 `max_backoff_seconds` 控制。DMIT 当前为 30、60、120、240、300 秒封顶。

统一模型示例：

```json
{
  "provider": "nexkr",
  "product_id": "1",
  "name": "KT-IPv4-1C2G",
  "category": "VDS",
  "region": "KR · KT / Zone 3",
  "price": "$38.00 USD",
  "billing_cycle": "1 month",
  "stock": null,
  "available": true,
  "url": "https://nexkr.sh/app/buy/kt-ipv4-1c2g",
  "specs": {"cpu": "1 dedicated vCPU", "ram": "2 GB"}
}
```

## 安装与启动

需要 Python 3.9+：

```bash
cd /root/projects/vps-stock-watch/vps-stock-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" \
  .venv/bin/playwright install --with-deps chromium
cp .env.example .env
```

在 `.env` 填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，不要提交真实 token。
随后启动：

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" \
  xvfb-run -a .venv/bin/python -m watcher.app --config config.yaml
```

只抓取一次并建立/更新本地基线但不调用 Telegram：

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" \
  xvfb-run -a .venv/bin/python -m watcher.app --config config.yaml --once --no-notify
```

默认全局间隔 60 秒；`POLL_INTERVAL_SECONDS` 可覆盖全局默认，Provider 自己的
`interval_seconds` 优先。DMIT 当前为 60 秒，以降低两分钟内短补货完全漏掉的概率；
不要设置成 10 秒等高频值，以免对商城和 Cloudflare 造成不必要压力。

## Telegram

Bot 使用标准 `sendMessage` 发通知，用 `getUpdates` 接收 `/status` 和 `/stock`。启动时通过
`setMyCommands` 注册：

- `/status`：查看监控状态；
- `/stock`：查询当前可购买库存。

`/status` 显示
Provider 数量、各 Provider 最近检查/成功时间、商品数、有货数、累计新品/通知数和最近
错误，并附带“⏱ 设置扫描间隔”按钮。该按钮使用 inline callback，不新增 `/interval`
等文字命令。一个 token 不应同时由另一个 getUpdates 消费者或 webhook 使用。

`/stock` 只读取最近成功扫描写入的 JSON state，不调用 Provider、不发起商城请求，也不
启动 Chromium。首页按当前 state 实时显示九个 Provider 的可购买数量；进入 Provider 后
按 state 中保存的真实分类列出全部当前可购买商品。每行只显示名称、价格/周期和直达
链接；长目录会自动拆成连续消息，优先在分类之间拆分，只有最后一条消息提供返回按钮。
刷新只重新读取磁盘 state。

商品的正常/隐藏属性来自 `metadata.discovery`，不按名称猜测：catalog/API/官网导航发现的
商品标记为“正常库存”，`extra_product_urls`、`extra_pids` 等补充入口标记为“隐藏库存”。
隐藏库存统一放在正常库存之后。Blossom 的 ISP/Metal `notification_suppressed` 只控制主动
通知，不影响 `/stock`，只要当前有货仍按正常库存显示。FACHOST Product 22 因来自
`extra_product_urls`，当前有货时显示为隐藏库存。

可购买筛选仍严格使用正整数库存或 `available=true`，不会把布尔库存伪造成具体数量。
最近成功时间超过当前 Provider 扫描间隔两倍时会提示数据可能过期，但仍
保留最后一次成功库存。命令消息和每一次 callback 都重新校验管理员 chat 与 sender。

扫描间隔菜单显示全部九个 Provider 的当前有效间隔。管理员选择商家后
直接输入整数秒数；允许范围为 10–86400 秒，输入 `17` 就会原样保存为 17 秒，没有预设
按钮或取整。只有 `.env` 中 `TELEGRAM_CHAT_ID` 对应的私聊用户同时匹配 callback 发送者
和消息 chat 时才可以修改，直接发送数字也不能绕过权限。

运行时覆盖值原子保存在已忽略的 `data/state.json` 内：
`settings.provider_intervals.<provider>`。设置成功后只重排目标 Provider 的下一次检查，
无需重启；服务或 VPS 重启后继续使用最后保存的值。未设置覆盖值时使用 `config.yaml`
中该 Provider 的 `interval_seconds`。调度器按 Provider 保存独立 deadline，并且同一个
Provider 完成当前抓取后才安排下一轮，不会堆积重入任务或多个 Chromium。

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_CHAT_ID=123456789
```

## YAML 规则 Provider

`config.yaml` 内含 CSS 示例。规则引擎支持：

- CSS：`{type: css, selector: ".price"}`，可加 `attribute: href`；
- XPath：`{type: xpath, expression: ".//span[@class='price']/text()"}`；
- Regex：`{type: regex, pattern: "Stock: (?P<value>\\d+)"}`；
- JSONPath：`{type: jsonpath, expression: "$.price"}`。

字段可以使用 `transforms: [strip, lower, int, float]`。`item_rule` 先提取商品项，
`fields` 再相对每项取值；缺少明确 `available` 时，可配置 `soldout_rule`，有数字库存时
也可仅提供 `stock`。URL 会相对 `catalog_url` 补全。

## 测试

```bash
.venv/bin/python -m unittest discover -v
```

测试不访问真实商家，也不调用 Telegram。当前完整测试集为 200 项，覆盖首次基线、新
SKU、补货、售罄、库存数字变化、价格/付款周期/名称变化、下架、抓取失败保留旧状态、
布尔库存不伪报数字变化、Misaka 的全球目录与港台日分层策略、九个内置 Provider 的发现与
完整目录保护、DMIT 调度/进程/超时隔离、Telegram API 错误脱敏，以及四种规则解析。

## systemd 服务

[`deploy/vps-stock-watch.service`](deploy/vps-stock-watch.service) 使用当前正式项目路径
`/root/projects/vps-stock-watch/vps-stock-watch`，并只允许服务写入项目的 `data/`。DMIT 的
headed Chromium 回退还需要系统安装 `xvfb` 和 `xauth`；
[`deploy/20-memory-guard.conf`](deploy/20-memory-guard.conf) 为同一服务设置 800M soft limit
和 900M hard limit，并委派 memory controller。应用会在支持 `DelegateSubgroup` 的新
systemd 或不支持该指令的旧 systemd 上，都把 watcher 放入叶 cgroup；DMIT 扫描使用独立
临时子 cgroup，其 700M soft limit 与 800M hard limit 由 `config.yaml` 控制；扫描超时或 OOM 只清理该轮
浏览器，不会停止 watcher 主循环。
Chromium 位于 unit 指定的共享路径：

```bash
PLAYWRIGHT_BROWSERS_PATH=/root/projects/vps-stock-watch/vps-stock-watch/.playwright-browsers \
  /root/projects/vps-stock-watch/vps-stock-watch/.venv/bin/playwright install chromium
```

Playwright/Chromium 只在目标站点阻挡普通 HTTP 时使用；当前主要用于 DMIT 的 Cloudflare
回退，其他可由公开 API 或服务端 HTML 完整获取的 Provider 不会启动浏览器。部署前检查
unit 中的项目路径，然后安装并启用同一个服务：

```bash
sudo cp deploy/vps-stock-watch.service /etc/systemd/system/
sudo install -d /etc/systemd/system/vps-stock-watch.service.d
sudo cp deploy/20-memory-guard.conf /etc/systemd/system/vps-stock-watch.service.d/
sudo systemctl daemon-reload
sudo systemctl enable --now vps-stock-watch.service
```

## 当前限制

- NexKr 公共 API 和 DMIT HTML 都只公开布尔库存，不提供具体数量。
- Blossom Host 的公开 catalog 提供每个 plan 的布尔状态和整数库存，Provider 监控完整
  `plans` 和独立 residential beta offer；若未来字段缺失，不会猜测库存数字。
- BOILCLOUD 当前没有公开 catalog API；Provider 依赖公开 WHMCS 分类 HTML、首页导航和
  sitemap。后台从未公开链接的草稿无法枚举，但所有公开导航分类、公开测试商品和未来
  新增到这些 discovery 入口的分类都会自动进入扫描。
- NexKr 未返回的真正后台草稿无法从公开前端发现。
- DMIT 是否需要浏览器取决于部署 IP 的 Cloudflare 判定；当前环境需要 Xvfb + headed
  Chromium 降级，且挑战策略变化后仍可能失败。失败会安全保留旧状态。
- 第一版状态是单个原子写入的 JSON，适合单进程个人监控，不支持多个实例同时写入。
