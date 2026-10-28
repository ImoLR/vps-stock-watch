# vps-stock-watch

一个面向多 VPS/IDC 商家的轻量监控器。Provider 只负责把站点转换成统一
`Product`；完整目录比较、JSON 状态、失败退避、Telegram 和 `/status` 都在核心层。

内置 NexKr、NewNB、DMIT、Blossom Host、BOILCLOUD、FACHOST、LeiKwanHost、利群汇聚、VMSILO 与 Misaka，
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
SKU。`metal`（Bare metal）仍完整抓取、比较并写入 state，也继续计入 `/status`，但所有
变化都标记为静默，不发送 Telegram。其余 family（`isp` ISP Line 及独立的 residential
inventory）自 2026-10-22 起只在新品（NEW）和下架（REMOVED）时通知；补货、售罄、库存
数字、价格和名称变化照常写入 state，但不发送（`BlossomProvider.NOTIFY_CHANGE_TYPES`）。
是否静默只按 `family` 判断，不读取旧 state 里保存的 `notification_suppressed`，所以旧
快照中 ISP 商品的下架也会通知。需要再静默或恢复某一系列时，只需调整
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
都完整成功后才会提交 snapshot 并允许 `removed_product`。每轮约 13 个请求，间隔为 600 秒，运行时仍可
通过 Telegram 输入 10 秒–24 小时之间的任意间隔（如 `90`、`10m`、`2h`）并原样持久化。

2026-10-18 起，BOILCLOUD 的补货、售罄和库存数字变化暂停发送 Telegram（仍照常写入 state，
`/status`、`/stock` 不受影响），只有两类商品例外：SeedNet 线路（名称、分类名或商品说明含
SeedNet，不分大小写，`Seed Net` 也算）和年付商品（卡片付款周期为年繳/annually，或名称、
分类名、说明含“年付/年繳”，“半年付”不算）。每个台湾分类的标题都写着
“HiNet/SeedNet/Kbro/…”，所以分类标题不参与判断，否则所有 HiNet 商品都会被算进去。新品、
下架和价格变化照常通知。分类卡片只显示一个付款周期，月付和年付并存的商品只有下单页才看得到
年付选项；Provider 不打开下单页，这种商品要靠名称或说明里的“年付”才能识别。恢复全部库存
通知时删除 `BoilcloudProvider.should_notify`，调整例外范围改 `STOCK_ALERT_TEXT`。

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
baseline；每轮约 5 个请求，扫描间隔为 300 秒，可从 Telegram 的独立 Provider 菜单修改并持久化。

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
每轮约 14 个请求，扫描间隔为 600 秒。

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

2026-10-28 起按用户要求在 `config.yaml` 中 `enabled: false` 停用：该门户一直没有公开商品，
LeiKwan 库存由 `leikwanhost` 扫描覆盖。代码保留，需要时改回 `enabled: true` 即可。

### VMSILO

VMSILO 位于 `https://portal.vmsilo.com/`，使用 WHMCS。2026-10-08 之后站点从 ShufyTheme
换成自定义 `bestvm` 主题：分类侧栏和 `standard__cart__Nproducts` 计数标记都已消失，
分类改为 `.bv-group-tabs` 中的 `/cart.php?gid=<N>`（会重定向到 `/store/<category>`），
商品卡为 `.bv-plan-grid > article.bv-plan#product<PID>`。没有发现公开 catalog JSON/API；
Provider 以普通 HTTP 读取 `/store` 的分类标签，再逐个打开其余分类，不启动 Chromium，
也不调用购物车 AJAX。首页现在直接重定向到商店，不再单独抓取；隐藏分类可通过
`extra_category_urls` 配置。

商品卡没有公开 WHMCS 数字 PID，购物车中的 `i=0/1/...` 又是会话内索引，不能作为 ID；
Provider 因此使用公开商品 URL 中稳定的 product slug，并以 `vmsilo:<slug>` 保存。购买链接
保留网站给出的 `/store/<category>/<product-slug>`，不猜测 PID URL。若页面公开精确
`N Available` 就保存整数，否则只依据可订购/售罄按钮保存布尔状态，绝不伪造库存 1。
售罄卡片若不再给出下单链接，会沿用本进程已学到的 PID→slug 映射保持同一 ID。

2026-09-27 的真实扫描发现 2 个侧栏分类：IEPL 有 6 个商品，socks5 以模板计数明确返回
0 个商品；6 个商品当前全部可订购，均只提供布尔库存，没有具体库存数字。首页旧 `cn2`
入口会重定向到 IEPL 并被去重；robots、sitemap 与搜索结果没有发现额外可购买的隐藏商品。
2026-10-14 新主题下的真实扫描：IEPL（gid=2）6 个商品与旧 slug、价格、规格完全一致，
socks5（gid=11）新增 3 个一次性 ¥0.00 商品，完整扫描只需 2 个 HTTP GET，默认间隔 120 秒。
分类标签少于 `minimum_categories`、某分类页标签不完整或高亮的不是所请求分类、缺少
`bv-products-head` 布局、请求失败都会拒绝整轮 snapshot，避免误报批量下架。

**踩坑（2026-10-22）：socks5 的 ¥0.00 只是卡片底价。** 用户登录后看到代理已有价格，
监控却一直记 ¥0.00、从未提醒。原因是 Provider 只读分类卡片，没打开下单页。实际情况
（未登录即可看到，全部公开）：

- 下单页 `/store/socks5/<slug>` 由 `qqdlproxy` 模块渲染，页面内嵌
  `<script id="qqdl-order-config">` JSON，列出子类型、时长、国家、城市、数量字段；
- 价格要用表单字段 POST `cart.php?a=confproduct&calctotal=true` 实时计算，商品行价格
  随时长/数量变化，各配置项本身显示 ¥0.00。当时测得：动态住宅 ¥45/GB（1–300 GB，
  流量不过期）；静态住宅 7 天 ¥45、30 天 ¥90、360 天 ¥810；静态机房 30 天 ¥67.5、
  360 天 ¥607.5；
- 能否下单看两处：变体上的 `purchasePaused`（当时只有动态“Unlimited traffic
  bandwidth”为 true），以及 POST `/modules/servers/qqdlproxy/stock.php`
  （`pid` + `token` + 当前 configoption）返回的 `status`/`allowed`
  （`available`/`scarce` 可下单，`unavailable` 不可）；
- 分类名一直叫“socks5-开发中禁止购买”，但上述开关和库存接口都已放行，分类名不能
  作为是否开卖的依据。分类名变化本身也不在通知范围内。

**现在的做法：** 卡片价格为 0 的商品，Provider 会用独立会话（打开商品页会把它放进
会话购物车，共用会话会越堆越多）以 `?language=chinese` 打开下单页，按每个子类型 ×
每个时长、数量 1、第一个国家、“国家内随机”城市逐档调用 `calctotal`，结果写入
`pricing`（如 `普通住宅 IP · 7 天`、`不限时流量 · 1 GB`），最低一档作为 `price`。
`purchasePaused` 的子类型不计价，记入 `metadata.order_page.paused`；全部暂停时商品记为
无货。实测国家和城市不影响价格，数量按倍数计算。下单页结构不认识、某档算不出价格
都会拒绝整轮 snapshot。完整计价约 25 个请求、15 秒，因此按
`order_page_refresh_seconds`（配置为 1800 秒）缓存，卡片价格变化时立即重算。新品和价格
变化通知会按网站顺序列出每一档价格。

库存接口只返回 `available`/`scarce`/`unavailable` 三种状态，不给具体数量；逐国家查询
需要几百个请求，目前不监控地区库存。

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
进行的促销。完整扫描 43 个普通 GET，约 2.6–2.8 秒，间隔为 7200 秒（2 小时，用户确认
Misaka 库存很少变化）。2026-10-22 起
`app.misaka.io` 对机房 IP（含浏览器）返回 Vercel `x-vercel-mitigated: deny`，家庭宽带
可正常访问；此前 60 秒一轮、每天约 6 万请求，已降到每天约 500。2026-10-28 起库存扫描和
Route Watch 都改走 Cloudflare 上的 `https://api.misaka.io/mc2`（同一份 JSON，未被封），
`app.misaka.io` 仍只用于给用户的购买链接。

Misaka Route Watch 与库存扫描完全独立，默认每 7200 秒（2 小时）使用
[Misaka 官方 Looking Glass](https://misaka.ping.sx/) 的公开 WebSocket MTR，从香港、台湾、
日本各自最高的已标注线路等级（当前均为 Premium Plus）主动测向广东电信、联通、移动
三个固定 IPv4 目标。节点来自官方 probe directory，并与 `/api/mc2/regions` 的 speedtest
地址交叉匹配；IPv6 只发现并保存，第一版不测量、不通知。线路状态单独原子写入
`data/route_state.json`，不会与库存 state 互相覆盖。

线路比较使用 Team Cymru DNS 查询得到的 ASN、组织和 prefix，并缓存结果；fingerprint 只取
去重后的 ASN 序列，因此单个超时、接口 IP、同 ASN 内 hop 数或 RTT 抖动不会触发通知。
首次成功只建立静默 baseline；新路径必须连续两轮相同才确认，单次 candidate 恢复
baseline 会被丢弃。2026-10-18 起，只有变化前或变化后的路径经过三网精品网络（半程、全程
都算）时，确认的变化才发 Telegram：电信 CN2（AS4809，GIA 和 GT 都算）、电信 CTG
（AS23764）、联通 9929（AS9929）、联通 10099（AS10099，CUG）、移动 CMIN2（AS58807）。精品
网络不变、只是中间 transit 改变也会通知；消息里的“精品线路”一行显示变化前后的精品网络。只
经过普通线路的变化照常更新 baseline，只写一条 INFO 日志；移动 CMI（AS58453）按普通线路处理。
CN2 GIA 和 CN2 GT 的 ASN 路径相同，不区分，两者互换也不会被识别为变化。精品网络清单在
`route_watch.PREMIUM_NETWORKS`。LG、目标可达性、schema 或 ASN 覆盖率失败时保留旧 baseline，并把已取得
的 raw attempt 仅作为诊断状态保存。Route scheduler 自带防重入和失败退避，不阻塞库存线程。
`/status` 显示 Route Watch 健康信息，并提供“Misaka 当前线路”只读按钮；点击只读 state，
不会立即发起 traceroute。

### NewNB Cloud

[nnb.ltd](https://nnb.ltd/dashboard/create) 是 Nuxt SPA，下单页调用公开的
`GET https://api.nnb.ltd/api/v1/nodes`（无需登录）。NewNB 不卖固定套餐：每个节点有资源
上下限、按单位计价的月费和若干网卡（NIC），每个 NIC 有自己的月费和流量。Provider 为每个
「节点 + 主网卡」生成一个 Product，价格取全部资源最低时的起步价（`base + NIC 月费`，
计价公式照搬站点 bundle）。接口没有库存数字，`stock=null`；节点 `orderable=true` 且
`status=active` 视为有货，售罄时节点被关闭或下架。一次 GET 约 0.7 秒、6 KB，间隔 60 秒。

`python -m watcher.nnb_autobuy` 是一次性的抢购程序：每 5 秒拉一次节点列表，发现名称/分组/
说明/网卡含关键词（默认 `港`、`HK`）且有移动入口（CM）的可下单节点后，按最低配置下单。
余额够就 `POST /user/services` 直接扣款开通；不够就 `POST /user/services/checkout` 创建
支付订单并把链接发到 Telegram（付款后才开通，不占库存）。移动入口要求账号已完成 KYC，并且
需要白名单地区（默认 `100000` 不确定地区）。账号来自环境变量 `NNB_USERNAME` /
`NNB_PASSWORD`，登录没有验证码，token 保存在 `data/nnb_token.txt`；成功后写
`data/nnb_autobuy_done.json` 并退出，之后不会再买。

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

### 接入或排查站点：必须点到下单页

列表页/分类页上的价格和状态不可信，接入新站点、新分类，或者用户说“网站上新了但没
收到通知”时，都要逐个商品点进下单/配置页，一直走到“继续/加入购物车”按钮前，确认：

- 真实价格从哪里来：卡片价、`configoption` 加价、还是页面 JS/AJAX 实时计算；
- 有哪些时长、地区、子类型、数量/流量档位，各自价格多少；
- 能不能下单由谁决定：按钮 disabled、`purchasePaused` 一类开关、库存查询接口，
  还是分类名里的“禁止购买”字样；
- 每个站点的下单页都不一样（NexKr 的 zone 级库存、VMSILO socks5 的配置项算价），
  不能套用上一个站点的结论。

只扫列表页漏掉过 VMSILO socks5 的真实价格（见下方 VMSILO 小节）。

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

**请求量原则（2026-10-22）：** 每轮只发 1 个请求的商家（NexKr、NewNB、DMIT、Blossom）
保持 60 秒；每轮要翻多页的商家放慢到每天约 2 千请求以内（BOILCLOUD、
LeiKwanHost 600 秒，FACHOST 300 秒，Misaka 库存与 Route Watch 均 7200 秒，VMSILO 下单页计价缓存 1800 秒）。
新增商家或改间隔时先算“每轮请求数 × 每天轮数”。Misaka 曾以每天约 6 万请求运行，
之后被 Vercel 防火墙拒绝。

**掉线通知：** 某商家连续失败至少 3 次且距上次成功超过 `outage_alert_after_seconds`
（默认 3600 秒）时，发一次“⚠️ 监控异常”；之后第一次成功发“✅ 监控已恢复”。
同一次故障只通知一次，状态记在 Provider 的 `outage_alerted_at`。

## Telegram

Bot 使用标准 `sendMessage` 发通知，用 `getUpdates` 接收 `/status`、`/stock` 和 `/autobuy`。
启动时通过 `setMyCommands` 注册：

- `/status`：查看监控状态；
- `/stock`：查询当前可购买库存；
- `/autobuy`：查看抢购程序（也可在 `/status` 点“🛒 抢购程序”）。

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
隐藏库存统一放在正常库存之后。Blossom 的 Metal `notification_suppressed` 只控制主动
通知，不影响 `/stock`，只要当前有货仍按正常库存显示。FACHOST Product 22 因来自
`extra_product_urls`，当前有货时显示为隐藏库存。

可购买筛选仍严格使用正整数库存或 `available=true`，不会把布尔库存伪造成具体数量。
最近成功时间超过当前 Provider 扫描间隔两倍时会提示数据可能过期，但仍
保留最后一次成功库存。命令消息和每一次 callback 都重新校验管理员 chat 与 sender。

扫描间隔菜单显示全部九个 Provider 的当前有效间隔。管理员选择商家后
直接输入间隔：纯数字或 `s` 结尾为秒，`m` 为分钟，`h` 为小时，可组合（`1h30m`），也接受
`30分钟`、`2小时`；允许范围 10 秒–24 小时，输入 `17` 原样保存为 17 秒，没有预设按钮或取整。
状态、菜单和设置结果都按“2 小时 / 10 分钟 / 45 秒”显示。只有 `.env` 中 `TELEGRAM_CHAT_ID` 对应的私聊用户同时匹配 callback 发送者
和消息 chat 时才可以修改，直接发送数字也不能绕过权限。

运行时覆盖值原子保存在已忽略的 `data/state.json` 内：
`settings.provider_intervals.<provider>`。设置成功后只重排目标 Provider 的下一次检查，
无需重启；服务或 VPS 重启后继续使用最后保存的值。未设置覆盖值时使用 `config.yaml`
中该 Provider 的 `interval_seconds`。Telegram 覆盖值优先于 `config.yaml`，所以在
`config.yaml` 改某个商家的间隔时，要同时删掉它的覆盖值，否则 TG 设置的旧值继续生效。调度器按 Provider 保存独立 deadline，并且同一个
Provider 完成当前抓取后才安排下一轮，不会堆积重入任务或多个 Chromium。

抢购程序（`watcher.nexkr_autobuy`、`watcher.nnb_autobuy`）是独立进程，各自每 30 秒以及
状态变化时原子写入 `data/autobuy/<name>.json`（目标、轮询间隔、当前库存状态、结果）。
主 Bot 读取这些文件：心跳超过 `max(30, 轮询间隔) × 3 + 30` 秒未更新显示为“进程未运行”。
目录可用 `autobuy_status_dir` 配置。

每个仍在运行的抢购程序都有“▶️ 启动 / ⏹ 停止”和“⏱ 间隔”按钮。Bot 只写
`data/autobuy/<name>.control`（`paused`、`interval`），抢购程序每轮重新读取：停止后进程
继续存活但不发任何请求（不登录、不拉目录）；间隔可输入 2–3600 秒，下一轮生效。抢到后
程序不再退出，而是自动停止并记录结果；之后再点启动会先要求确认（会再买一台并扣款），
确认后旧的 `*_done.json` 改名归档，重新开始抢购。

```dotenv
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_CHAT_ID=123456789
```

## 抢购程序：重新开抢与踩过的坑

两个抢购程序都只为一次活动写的，抢完后服务和账号凭据都已清理，代码保留下来供下次复用。

**重新开抢**：账号写进 `data/<站点>_account.env`（`chmod 600`；NexKr 用
`NEXKR_EMAIL`/`NEXKR_PASSWORD`，NewNB 用 `NNB_USERNAME`/`NNB_PASSWORD`）。
如果旧的 `*_done.json` 还在，先改名归档，否则程序不会再买。然后用临时 unit 启动：
它不会随重启自动恢复，活动结束后 `systemctl stop` 就会消失。

```bash
cd /root/projects/vps-stock-watch/vps-stock-watch
systemd-run --unit nexkr-autobuy --working-directory "$PWD" \
  -p EnvironmentFile="$PWD/.env" -p EnvironmentFile="$PWD/data/nexkr_account.env" \
  -p MemoryMax=600M -p Restart=on-failure -p RestartSec=1min \
  /usr/bin/xvfb-run -a "$PWD/.venv/bin/python" -m watcher.nexkr_autobuy \
  --slug <商品 slug> --interval 5 --code <优惠码>
# NewNB 不需要浏览器：
#   … -m watcher.nnb_autobuy --interval 5   （MemoryMax=300M，不用 xvfb-run）
```

正式开抢前先用 `--dry-run` 跑一轮；NexKr 可先用 `--login-only` 确认能登录。
抢完后 `systemctl stop <unit>`，删除 `data/<站点>_account.env` 和 cookie/token 文件。

**NexKr 的坑（2026 国庆活动）**

- 登录有 reCAPTCHA v3，只能用 Playwright 起有界面的 Chromium 登录，所以要套 `xvfb-run`。
  下单和用余额付款不需要验证码。
- 程序自动登录会把用户自己在网站上的登录挤掉。凡是需要用户去网站上操作（付款失败、
  余额不足、有货时掉登录），程序都会创建 `data/nexkr_manual_login` 并停止自动登录；
  用户处理完后删掉这个文件就会恢复。
- 目录里会出现 `in_stock: true`，但所有区都在 `zone_sold_out` 里。最早把售罄区过滤掉后
  没有区可选，下单不带区，返回 400“请选择区域”；后来改成也去售罄区下单，每 5 秒一次，
  两天里打出了约 1.17 万个 409。现在只在没标售罄的区下单；全部售罄时只看目录、
  不调用下单接口。每轮重新读取区域，所以商品换区也能跟上。
- 优惠码只要有一个被拒，整单就会失败（400 `coupon_rejected`）。所以缺货期间每 10 分钟
  用 `/orders/quote` 预检一次，丢掉被拒的码；下单时如果还是 `coupon_rejected`，
  就去掉优惠码重下。SMSVERIFYFEE 和 WELCOME@NEXKR 当时可以叠加。
- 下单后余额不足时保留订单，并发 Telegram 让用户手动付款。
- Telegram 写的 `data/autobuy/<name>.control` 里的 `interval` 会覆盖命令行的 `--interval`。

**NewNB 的坑（2026-10 沪港 CM 上架）**

- 登录没有验证码。移动入口要求账号已完成 KYC，还要选白名单地区（默认 `100000`
  不确定地区）。
- 余额不够时只能创建支付订单；付款前不开通，也不占库存，所以要提前充好余额。
- 刚上架时买到的那台是管理员测试，几分钟后被终止退款；后来在 Telegram 里重新启动才
  真正买到。抢到后要去面板确认服务没有被终止。
- CM 入口从海外 SSH 连不上，开通后要在国内测试登录。

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
