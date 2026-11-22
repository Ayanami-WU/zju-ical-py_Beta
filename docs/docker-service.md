# Docker 多用户订阅服务

本服务将现有课表/考试导出能力包装为 Web 管理页和独立刷新 worker。原来的 `zjuical.py` 和单用户 `webical.py` 继续保留；Docker 服务不执行 `git pull`，更新时重新构建镜像。

## 认证方式

**本服务不接收浙大统一身份认证密码，也不使用 `iPlanetDirectoryPro` 全局 SSO Cookie。** 用户只在浙大官方网页输入密码，完成登录后，把教务系统专用的会话凭据交给本服务。

交互参考了 [NeverRun-fe](https://github.com/5dbwat4/NeverRun-fe/blob/5805beead59994af3249e0a050f56454a228aafd/src/pages/first-time.vue) 的 Token 入口，未复制其代码。它使用的体育系统 JWT 无法查询本项目的教务接口。

| 学生类型 | 接收的凭据 | 如何获取 |
| --- | --- | --- |
| 本科生 | `zdbk.zju.edu.cn` 的 `JSESSIONID`（路径 `/jwglxt`）、另一条 `JSESSIONID`（路径 `/`）及 `JSESSIONTOKEN`；若存在负载均衡 `route` 也可填写 | 在开发者工具的“应用/存储 → Cookie → zdbk.zju.edu.cn”中按路径分别复制。2026-11-17 真实查询确认当前会话需要两条不同路径的会话及教务 JWT，并使用 AJAX 请求头；不需要全局 `JWTUser` 或 `JSESSIONPREJSDM` |
| 研究生 | `yjsy.zju.edu.cn` 的 `X-Access-Token` | 在官方系统登录后，从开发者工具的网络请求头中复制值 |

不要粘贴完整 Cookie、请求头、密码、全局 SSO Cookie 或体育系统 JWT。普通网页受浏览器同源策略限制，无法自动读取学校 Cookie。可以手动复制会话，也可以使用下方本机助手生成并导入会话文件；密码既不经过本服务前端表单，也不发给本服务后端。

### 本机账号密码登录

手机用户可优先使用下一节的 Cookie-Editor 路径，无需运行 Python。

网页“用本机账号密码获取会话”入口提供可审阅源码的登录助手压缩包。下载、解压后，在该目录打开终端运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m server.local_login
```

仓库开发环境可先安装 `requirements-login.txt`，然后执行最后一条命令。研究生添加 `--kind grs`。Windows 使用 `.venv\Scripts\python.exe` 替代 `.venv/bin/python`。

在交互终端输入学号与不回显的密码；不支持通过参数、环境变量或管道传入密码。助手只连接学校固定 HTTPS 域名，校验证书并约束每次请求和重定向，登录子进程最多运行 75 秒且不自动重试。进程结束后不保留认证 Cookie jar；只将白名单内的教务专用会话写入本机 `teaching-session.local.json`，文件权限为 `0600`，拒绝覆盖已有文件。现有文件请移走，或使用 `--output` 指定新文件名。

网页选择“导入本机会话文件”后，只在本机解析并填写表单；检查学号和订阅设置，同意保存并提交后才上传教务会话。文件不含密码、全局 `iPlanetDirectoryPro` 或 `JWTUser`，但仍可用于访问教务系统，导入后可自行删除。服务器下载包仅包含明确选择的公开源码、配置及许可证，不包含部署密钥、数据库或本机文件。

学校若要求验证码、二次认证或改变登录流程，助手会停止；请使用官方页面登录后手动提供会话。本机进程会短暂处理密码，无法保证抵御已被控制的电脑，也不宣称 Python 字符串可被可靠清零。此路径不提供无人值守的密码重新登录，教务会话过期后需要重新运行助手或更新会话。

### 手机一次导入（本科教务）

复用 [Cookie-Editor](https://cookie-editor.com/) 的 JSON 导出格式，兼容其官方 [iOS Safari 版本](https://apps.apple.com/app/id6446215341) 和 [Android Firefox 扩展](https://addons.mozilla.org/firefox/addon/cookie-editor/)。这是独立的开源扩展，无需我们分发或修改其代码。本功能不依赖远程调试或手机开发者工具。

1. 在手机安装上述官方扩展，仅允许其访问 `zdbk.zju.edu.cn`。
2. 在同一浏览器通过学校官方页面登录，进入本科教务页面。
3. 在该页面打开 Cookie-Editor，选择导出 **JSON** 并复制。
4. 回到订阅网页的“手机登录：一次导入会话”，粘贴并点击“读取并填写会话”。
5. 填写学号，检查学期、导出范围和保存同意后创建订阅。

整包只在浏览器本机解析。筛选器仅保留 `zdbk.zju.edu.cn` 下按路径匹配的两条 `JSESSIONID`、`JSESSIONTOKEN` 和 `route`，不返回或上传原始 JSON、`JWTUser`、`iPlanetDirectoryPro`、其他域名或未知 Cookie。筛选器忽略过期和带分区信息的 Cookie；有效会话值冲突或缺少必要路径时会报错，不会根据数组顺序猜测。解析完成立即清空原始输入框；创建前不会自动提交。Android Chrome 不支持该扩展路径，请使用上述受支持的 Firefox；研究生的 `X-Access-Token` 仍需手动获取或通过本机助手生成。

扩展具有读取所授权网站会话的能力，只授予教务网站所需权限。JSON 仍属于敏感会话，请勿发到聊天或分享给他人，导入后可清理剪贴板。当前验证覆盖解析器、公开下载包和桌面浏览器界面，手机实机和真实账号认证需用户联调。

专用会话仍具有对应教务系统中的账户权限，不是学校签发的“只读课表授权”。服务只调用项目已有的课程和考试查询接口，并通过 TLS 验证连接。学校可能限制会话时效、来源 IP 或调整 Cookie 机制；跨设备复用是否可用及有效期仍需用本人账号验证。服务不能延长学校会话有效期，会话过期后需在管理页重新提供。

目前支持课程和本科生考试；研究生考试接口仍未适配。学在浙大待办需要独立会话，服务版暂不提供，原 CLI 的 `--include-todos` 保留。

## 启动

需要 Docker Compose v2。仓库目录内执行：

```sh
python3 tools/init_service.py
```

该命令创建 `.secrets/zjuical.key` 和 `.env`，已有文件不覆盖，不显示密钥内容。密钥所在目录权限为 `0700`；为供容器内 UID 10001 读取，单个密钥文件为 `0644`，通过 Compose secret 只读挂载。不要将密钥放进镜像、公开目录或提交到 Git。生产环境也可由部署系统提供 secret 文件。

编辑 `.env`，填写自己的域名：

```dotenv
PUBLIC_URL=https://calendar.example.com
SITE_ADDRESS=calendar.example.com
COOKIE_SECURE=true
SYNC_INTERVAL=3600
```

域名解析到部署服务器后，启动内置 Caddy HTTPS 入口：

```sh
docker compose --profile https up -d --build
docker compose ps
```

打开 `PUBLIC_URL`。Caddy 使用 80/443 端口自动申请证书；这些端口需可访问。Web 端口只映射到宿主机 `127.0.0.1:5273`。如果已有 HTTPS 反向代理，使用 `docker compose up -d --build web worker`，将代理指向该端口。反向代理应保留正确的 Host，并关闭含完整请求 URI 的访问日志，或对 `/calendar/` 后面的密钥脱敏。

默认不信任 `X-Forwarded-For`。仅当 Web 入口完全位于一个受信反向代理之后，且代理覆写客户端 IP 头时，可设置 `TRUSTED_PROXY_HOPS=1`；否则保持 `0`。默认下代理后的用户共享 IP 频率限制。

只在本机试运行时，可以设置：

```dotenv
PUBLIC_URL=http://127.0.0.1:5273
COOKIE_SECURE=false
```

此时启动 `web worker`，不要启用 HTTPS profile。程序拒绝在公网 HTTP 地址上关闭 Secure Cookie。

本次本机验证使用独立的 `.env.local` 和 Compose 项目名，已在 OrbStack 中运行：

```sh
docker compose --env-file .env.local -p zjuical-local up -d --build web worker
```

本机入口为 `http://127.0.0.1:5273`；该地址只适合在这台电脑测试，手机订阅或公网托管需配置上面的 HTTPS 域名。

## 用户使用流程

1. 选择学生类型，在学校官方页面登录。
2. 填入学号与教务系统专用会话，选择学期和导出范围，同意加密保存会话。
3. 创建后立即保存**管理密钥**。此密钥仅显示一次，用于换设备找回订阅；页面的管理会话使用 HttpOnly Cookie，不把密钥写入 localStorage。
4. 等待首次同步成功，复制私人订阅 URL，在 Apple 日历中选择“添加订阅日历”。Mac 可选“文件 → 新建日历订阅”。
5. 云端每小时尝试刷新。客户端按自身订阅刷新周期获取最新快照，不承诺即时推送。

订阅链接示例：`https://calendar.example.com/calendar/<43字符随机密钥>.ics`。订阅密钥、管理密钥与浏览器会话均来自独立的 32 字节随机数。订阅链接只可读取该份课表，不能管理订阅或读取学校会话。管理页可撤销旧链接、生成新链接、更新会话、重置管理密钥或删除订阅。重置管理密钥同时注销其他设备的管理会话。

**修改 URL 中的随机密钥不是给同一个订阅“更新凭据”**：请用管理页更新学校会话，订阅 URL 会保持不变。只有主动重置订阅链接时，才需要在日历 App 中重新订阅。

## 更新与故障处理

- Web 只读加密缓存，不在日历客户端下载时触发学校登录/采集。首次采集未成功时返回 503 和 Retry-After，避免把空日历作为成功结果交给客户端。
- 有缓存时刷新失败继续提供最近成功的数据。重试间隔为 15、30 分钟；连续三次失败后暂停，用户可更新凭据或手动重试。
- 每个采集子进程默认最多 180 秒，单个学校 HTTP 请求有连接/读取超时；worker 之间通过数据库租约互斥。进程崩溃后租约到期可恢复。
- 本科会话首次没有 `route` 时，学校负载均衡可能把请求送到不匹配的节点并返回 901，同时下发匿名 `JSESSIONID`。首次连接最多尝试三个独立会话，每次从用户提供的 Cookie 重新开始；成功完成全部查询后，只把学校下发的 `route` 加密写回凭据，后续刷新固定复用该节点。已知节点上的认证失败不做这类重连。考试与课程接口分别使用对应页面的 Referer。
- 用户更新凭据或删除订阅后，正在运行的旧任务不能覆盖新设置或恢复被删除的数据。
- 不同用户通过独立随机管理密钥与服务端会话隔离，学号不是管理权限凭证。研究生接口尚未提取到可校验身份的学号，所填学号只用作显示，不用于接管或合并其他用户订阅。
- 课程 UID 使用选课课号、原教学日期及节次，考试使用选课课号及考试类型，作业优先使用来源 ID；不随教师、教室、学分或考试时间的修改而改变。服务再加入订阅自身的命名空间。首次从旧 UID 迁移时，应使用专门的订阅日历，避免将新版文件反复导入已有个人日历。

更新部署：

```sh
git pull
docker compose --profile https up -d --build
```

如使用自己的反向代理，省略 `--profile https`。学期配置随镜像发布，管理员也可构建前更新 `configs/`，不接受用户提交任意服务器文件路径或远程接口 URL。

## 数据、备份与运维边界

SQLite WAL 数据在 `calendar-data` 卷；学校会话、完整日历缓存和可恢复的订阅密钥使用 Fernet 认证加密，管理密钥及浏览器会话令牌只保存 SHA-256 摘要。IP 频率限制键使用安装密钥 HMAC，数据库不保存原始 IP。服务在启动时校验密钥与数据库是否匹配。

单独备份 `.secrets/zjuical.key`，并使用 SQLite 在线备份或暂停 web/worker 后备份整个数据卷（含可能存在的 WAL 文件）。不要只在运行时复制主数据库文件。恢复时必须使用匹配的原密钥；丢失密钥无法恢复凭据和课表。本版本不提供自动主密钥轮换，勿直接替换密钥。删除订阅是逻辑删除，也需要依照自己的备份保留政策处理历史备份。

加密保护静态数据，无法防止已控制运行中服务器的管理员读取应用解密后的会话。若不信任托管者，后续应采用浏览器本地采集、只上传生成日历的方案。

服务有持久化创建/登录频率限制、CSRF 与 Origin 校验、安全 Cookie、CSP、固定公共 URL 和请求体大小限制。容器使用普通用户、只读根文件系统、独立数据卷和 secret，无需挂载 Docker socket。worker 关闭原采集器日志，不输出学校响应、个人信息或凭据；Web 不启用 URL 访问日志。

本地检查可使用：

```sh
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check .
docker compose config --quiet
```

本次已在 OrbStack 实际构建并启动 Web/worker，Web 健康检查通过。64 项测试分别在本机 Python 3.14 和镜像内 Python 3.13 断网环境通过；容器 HTTP 测试验证了创建、加密缓存读取、条件 GET/HEAD、旧链接撤销和重启后的数据/管理会话持久化，虚拟测试记录已清除。网页验证了表单加载与创建后的订阅/管理密钥界面。

2026-11-17 使用用户自愿提供的教务域专用会话完成真实联调，验证了两条不同路径的 `JSESSIONID`、`JSESSIONTOKEN`、AJAX 请求头、考试页面上下文与节点 Cookie 的保持。该账号生成 184 条事件（其中 9 条考试），UID 全部唯一；再次真实刷新仍为 184 条，UID 集合不变，且复用了已加密保存的节点。未读取统一认证密码或全局 SSO Cookie。这只验证本次本机 Docker 环境，不保证其他会话、网络和未来学校接口机制相同。

Mac 日历已添加独立的“ZJU 课程与考试（本机订阅）”，位置确认为“我的 Mac”，自动刷新为每 5 分钟。本机 HTTP 的“不安全连接”提示由用户手动确认。客户端更新测试结果：

| 操作 | 实测结果 |
| --- | --- |
| 加入临时测试事件 A 后刷新 | 显示 1 条 A，真实课程仍在 |
| 保持 UID，将标题及地点改为 B 后刷新 | 只显示 1 条 B，旧 A 消失 |
| 内容不变再次刷新 | 仍只有 1 条 B，无副本 |
| 服务端移除临时事件后刷新 | Mac 上临时事件为 0，真实课程仍可见 |

测试后服务端恢复为 184 条真实事件，临时加密备份已清除。验证了手动刷新触发的客户端同步；未等待完整 5 分钟自动刷新周期，生产 HTTPS 和手机等跨设备订阅仍未验证。回环地址仅适用于运行本服务的这台 Mac。
