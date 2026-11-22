# koala3 预生产 external 部署

入口为 `https://ical.koala-studio.org.cn`。Web 与 worker 在 koala3 同机共享本地 SQLite WAL；Axiops 仅声明 external 端点和路由，现有 Traefik 提供 HTTPS。两个容器的创建、重启、备份和升级由宿主 Compose 管理，Axiops 不向它们注入密钥，也不自动升级它们。

此部署是新的预生产实例。Mac 上 `zjuical-local` 的数据、密钥和订阅不会自动迁入；原本的 `127.0.0.1` 订阅也不会自动改成公网域名。

## 发布文件

先构建并发布镜像，记录注册表返回的实际摘要。使用同一摘要渲染 Web 和 worker：

```sh
python tools/render_external.py --image registry.koala-studio.org.cn/depart-software/deploy-manifests/zjuical@sha256:<实际摘要> --output /tmp/zjuical-release
```

输出可直接放入 `depart-software/deploy-manifests` 对应路径：

- `stateful/koala3/zjuical-web-preproduction.yaml`
- `stateful/koala3/zjuical-worker-preproduction.yaml`
- `software-department/infrastructure/preproduction/external-zjuical.yaml`

每份 Compose 只有一个服务，项目名均为 `zjuical-preproduction`，共享同一个由 Compose 管理的 bridge 网络。worker 可以独立启动，不依赖另一文件中未定义的服务。镜像必须是指定注册表的不可变摘要；渲染器拒绝覆盖已有输出。

路由贡献给预生产 `traefik.routes` 集合，再由现有逻辑资源转发给公共入口。不新增网关、不修改其他站点路由、不为 `/calendar/` 添加交互登录。当前入口不启用 URI 访问日志，应用也不记录订阅 URL 或请求体。保持 `TRUSTED_PROXY_HOPS=0`，代理后的用户暂时共享 IP 频率限制。

## 初始化与启动

私密状态仅保存在目标机：

| 路径 | 权限 | 用途 |
| --- | --- | --- |
| `/data/services/zjuical-preproduction/data` | `10001:10001`，`0700` | SQLite、WAL 与加密缓存 |
| `/etc/koala-stateful/zjuical-preproduction/zjuical.key` | `10001:10001`，`0400` | Fernet 主密钥，只读挂载 |

`tools/init_external.py` 是标准库脚本，可在目标机以 root 执行；也可在实际服务镜像内以 root 执行，仅挂载上述两个路径的父目录，脚本通过标准输入传入。它保留已有密钥；数据非空而密钥缺失时拒绝初始化。不要把生产密钥、Cookie、数据库或完整订阅链接写入仓库或公开构建日志。升级时不重新初始化数据。

在放置好文件的部署仓库中执行：

```sh
docker compose -f stateful/koala3/zjuical-web-preproduction.yaml config --quiet
docker compose -f stateful/koala3/zjuical-worker-preproduction.yaml config --quiet
docker compose -f stateful/koala3/zjuical-web-preproduction.yaml up -d --wait
docker compose -f stateful/koala3/zjuical-worker-preproduction.yaml up -d
curl --fail -H 'Host: ical.koala-studio.org.cn' http://172.26.20.3:15273/healthz
```

Web 仅绑定业务 IP `172.26.20.3:15273`，worker 不发布端口；上级网络访问策略应限制业务 VLAN 的可达范围。容器使用 UID 10001、只读根目录、移除 capabilities、禁止提权，不挂载 Docker socket。启动文件使用 `create_host_path: false`，私密文件缺失时直接失败。

验证本机入口健康后再提交 external 资源，使现有 CI 投递预生产期望状态。域名必须解析到公共入口，入口证书必须覆盖域名；最后检查 HTTPS 页面、健康检查、安全 Cookie 以及无效订阅返回 404。仅创建空实例无法验证学校会话有效性，真实课表获取需用户在 HTTPS 页重新提供教务会话。

## 备份、升级与回滚

数据库与主密钥必须配对恢复，但密钥应单独保管。运行中的 SQLite 必须使用在线备份 API；或先分别停止 worker 和 Web，再备份整个数据目录，包括尚未合并的 WAL。不要只复制运行中的主数据库文件。备份可能含加密的学校会话和课表，仍按私密数据管理。

升级前保留旧摘要及匹配的状态备份；将两份 Compose 的镜像同时替换为新摘要，先更新 Web 并等待健康，再更新 worker。不要使用 `down -v`、`--remove-orphans`，不要删除共用数据。回滚时恢复两份 Compose 的旧摘要并重新 `up -d`；若未来引入不兼容的数据库迁移，先停两个进程并恢复配对的数据/密钥备份。本次不变更现有服务数据库格式。

撤下预生产时，先撤销 Axiops 的 external 路由，再分别停止这两个服务；保留数据和主密钥。删除数据、密钥或备份需要另行明确操作。
