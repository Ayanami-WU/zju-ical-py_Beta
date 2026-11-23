import hmac
import json
import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from server.capture_script import capture_script
from server.ics_sources import SourceError, normalize_ics, validate_url
from server.login_bundle import local_login_bundle
from server.settings import ROOT, Settings
from server.store import Store, digest


def validated_input(data):
    if not isinstance(data, dict):
        raise ValueError("请提供有效的表单")
    username = data.get("username", "")
    mode, kind = data.get("mode"), data.get("kind")
    secret = data.get("secret", "")
    if not isinstance(username, str) or not re.fullmatch(r"\d{5,20}", username):
        raise ValueError("请输入有效学号")
    if kind not in {"ugrs", "grs"} or mode != "token":
        raise ValueError("服务仅接收教务系统专用会话，不接收密码或统一认证会话")
    if not isinstance(secret, str) or not 1 <= len(secret) <= 4096:
        raise ValueError("教务会话凭据为空或过长")
    if mode == "token" and (secret != secret.strip() or any(c.isspace() for c in secret)):
        raise ValueError("仅粘贴凭据值，不包含 Cookie 名称、空格或换行")
    cookies = {}
    for key, maximum in [("route", 512), ("root_session", 4096), ("teaching_token", 4096)]:
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError("教务 Cookie 过长或格式无效")
        if value and (kind != "ugrs" or any(c.isspace() for c in value)):
            raise ValueError("额外 Cookie 仅用于本科教务，请分别填写单个值")
        cookies[key] = value
    all_values = secret + "".join(cookies.values())
    if any(c in all_values for c in '\r\n;,"\\') or not all_values.isascii():
        raise ValueError("仅填写单个 Cookie 或 Token 的值，不包含完整请求头")
    if data.get("consent") is not True:
        raise ValueError("请先同意加密保存教务会话以进行定时更新")
    export = data.get("export", "all")
    if export not in {"all", "courses", "exams"}:
        raise ValueError("未知导出范围")
    config_name = data.get("config", "config.json")
    allowed = {p.name for p in (ROOT / "configs").glob("config*.json")}
    if not isinstance(config_name, str) or config_name not in allowed:
        raise ValueError("未知学期配置")
    include_todos = data.get("include_todos", False)
    if not isinstance(include_todos, bool):
        raise ValueError("待办选项必须为布尔值")
    if include_todos:
        raise ValueError("服务版暂不支持学在浙大待办的独立认证；可继续使用 CLI 导出待办")
    if kind == "grs" and export == "exams":
        raise ValueError("研究生考试接口尚未适配，请选择课程")
    return (
        {"username": username, "kind": kind, "mode": mode, "secret": secret, **cookies},
        {"export": export, "config": config_name, "include_todos": include_todos},
    )


def create_app(settings=None):
    settings = settings or Settings.from_env()
    store = Store(settings)
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16384)
    app.extensions["store"] = store
    app.extensions["settings"] = settings
    if settings.proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=settings.proxy_hops)
    cookie_name = "__Host-zjuical" if settings.cookie_secure else "zjuical-dev"

    def limited(category, maximum, seconds):
        if not store.limit(category, request.remote_addr or "unknown", maximum, seconds):
            abort(429, description="请求过于频繁，请稍后再试")

    def attach_session(owner=None):
        token, csrf = store.new_session(owner)
        g.new_cookie = token
        g.session = {"owner": owner, "csrf": csrf}

    def require_owner():
        if not g.session or not g.session["owner"]:
            abort(401, description="请先使用管理密钥登录")
        owner = g.session["owner"]
        if store.get(owner) is None:
            abort(401)
        return owner

    @app.before_request
    def security():
        if request.path == "/api/local-capture":
            request.max_content_length = 524288
        elif request.path == "/api/sources":
            request.max_content_length = 2097152
        g.new_cookie = None
        g.clear_cookie = False
        g.session = None
        if request.path.startswith("/api/"):
            limited("api", 120, 60)
            token = request.cookies.get(cookie_name, "")
            g.session = store.session(token) if token else None
            if request.method == "POST":
                if request.headers.get("Origin") != settings.public_url:
                    abort(403, description="请求来源校验失败")
                csrf = request.headers.get("X-CSRF-Token", "")
                if not g.session or not hmac.compare_digest(csrf, g.session["csrf"]):
                    abort(403, description="会话已过期，请刷新页面")
                if not request.is_json:
                    abort(415)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if getattr(g, "new_cookie", None):
            response.set_cookie(
                cookie_name,
                g.new_cookie,
                secure=settings.cookie_secure,
                httponly=True,
                samesite="Strict",
                max_age=settings.session_ttl,
                path="/",
            )
        if getattr(g, "clear_cookie", False):
            response.delete_cookie(
                cookie_name,
                secure=settings.cookie_secure,
                httponly=True,
                samesite="Strict",
                path="/",
            )
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        # Never echo request paths (which can contain subscription secrets).
        message = error.description if error.code in {400, 401, 403, 429} else "请求无法完成"
        return jsonify(error=message), error.code

    @app.errorhandler(ValueError)
    def bad_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(SourceError)
    def invalid_source(error):
        messages = {"unsafe_url": "请使用公网 HTTPS ICS 链接（443 端口），不支持内网、登录凭据或片段标识",
                    "invalid_ics": "ICS 无效或过大：请提供单个 UTF-8 iCalendar 2.0 文件，事件需有 UID 和开始时间；最多 512 KiB"}
        return jsonify(error=messages.get(error.code, "ICS 来源暂时不可用")), 400

    @app.errorhandler(Exception)
    def internal_error(error):
        # Upstream bodies, SQL, request headers and tokens must not enter logs or responses.
        app.logger.error("Internal service error: %s", type(error).__name__)
        return jsonify(error="服务暂时不可用，请稍后再试"), 500

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def health():
        with store.connection() as db:
            db.execute("SELECT 1")
        return jsonify(status="ok")

    @app.get("/downloads/local-login.zip")
    def download_local_login():
        return Response(
            local_login_bundle(), content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="zjuical-local-login.zip"'},
        )

    @app.get("/downloads/zjuical-capture.user.js")
    def download_capture_script():
        return Response(capture_script(settings.public_url), content_type="application/javascript; charset=utf-8")

    def state():
        result = {"csrf": g.session["csrf"], "subscription": None}
        owner = g.session["owner"]
        row = store.get(owner) if owner else None
        if row:
            credentials = store.decrypt(row["credentials"])
            token = store.decrypt(row["feed_secret"])
            sources = store.sources(owner)
            result["subscription"] = {
                "username": credentials["username"],
                "kind": credentials["kind"],
                "mode": credentials["mode"],
                "options": json.loads(row["options"]),
                "url": f"{settings.public_url}/calendar/{token}.ics",
                "updated": row["updated"],
                "error": row["error"],
                "paused": row["failures"] >= 3,
                "ready": row["calendar"] is not None or any(s["calendar"] is not None for s in sources),
                "syncing": row["lease_until"] > time.time(),
                "sources": [{"id": s["id"], "kind": s["kind"], "name": store.decrypt(s["name"]),
                             "host": urlsplit(store.decrypt(s["url"])).hostname if s["url"] else None,
                             "ready": s["calendar"] is not None, "updated": s["updated"], "checked": s["checked"],
                             "error": s["error"], "queued": s["kind"] == "url" and s["due"] <= time.time(),
                             "syncing": s["lease_until"] > time.time()} for s in sources],
            }
        return result

    @app.get("/api/session")
    def session_state():
        if g.session is None:
            limited("sessions", 30, 3600)
            attach_session()
        return jsonify(state())

    @app.get("/api/configs")
    def configs():
        result = []
        for path in sorted((ROOT / "configs").glob("config*.json")):
            data = json.loads(path.read_text())
            result.append(
                {
                    "file": path.name,
                    "label": ", ".join(data["classTerms"]),
                    "default": path.name == "config.json",
                }
            )
        return jsonify(configs=result)

    @app.post("/api/subscriptions")
    def create():
        if g.session["owner"]:
            abort(409)
        limited("create", 5, 3600)
        credentials, options = validated_input(request.get_json())
        owner, key = store.create(credentials, options)
        store.drop_session(request.cookies.get(cookie_name, ""))
        attach_session(owner)
        return jsonify(**state(), management_key=key), 201

    @app.post("/api/login")
    def login():
        limited("login", 10, 900)
        data = request.get_json()
        key = data.get("key", "") if isinstance(data, dict) else ""
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
            abort(401, description="管理密钥无效")
        owner = store.owner_for_key(key)
        if owner is None:
            abort(401, description="管理密钥无效")
        store.drop_session(request.cookies.get(cookie_name, ""))
        attach_session(owner)
        return jsonify(state())

    @app.post("/api/local-capture")
    def upload_capture():
        limited("capture", 12, 3600)
        data = request.get_json()
        if (not isinstance(data, dict) or set(data) != {"capture", "export", "consent"}
                or data.get("consent") is not True
                or data.get("export") not in {"all", "courses", "exams"}):
            raise ValueError("请确认上传并替换课表快照；此模式需要在手机主动更新")
        owner = g.session["owner"]
        old = store.get(owner) if owner else None
        if owner and old is None:
            abort(401)
        if not owner:
            limited("create", 5, 3600)
        identity = owner or secrets.token_urlsafe(32)
        now = time.time()
        modified = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = {"capture": data.get("capture"), "export": data["export"], "id": identity,
                   "sequence": int(now), "modified": modified,
                   "created": datetime.fromtimestamp(old["created"], UTC).strftime("%Y%m%dT%H%M%SZ") if old else modified}
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "server.capture"], input=json.dumps(payload, ensure_ascii=False), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=True, cwd=ROOT,
            )
            result = json.loads(completed.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError):
            raise ValueError("本机采集内容转换失败，已保留原课表，请重新采集") from None
        calendar = result.get("calendar")
        if not isinstance(calendar, str) or not calendar.startswith("BEGIN:VCALENDAR"):
            raise ValueError("采集内容无效或不完整，已保留原课表；请重新采集所选学期")
        credentials = {"username": result["username"], "kind": "ugrs", "mode": "snapshot"}
        options = {"config": result["config"], "export": data["export"], "include_todos": False}
        if old:
            original = store.decrypt(old["credentials"])
            if (original["username"], original["kind"]) != (credentials["username"], "ugrs"):
                raise ValueError("更新时请使用原学号；切换账号请新建订阅")
            if not store.replace_snapshot(owner, credentials, options, calendar, old["version"]):
                abort(409)
            return jsonify(state())
        owner, key = store.create(credentials, options, owner=identity, calendar=calendar, manual=True)
        store.drop_session(request.cookies.get(cookie_name, ""))
        attach_session(owner)
        return jsonify(**state(), management_key=key), 201

    @app.post("/api/settings")
    def update():
        owner = require_owner()
        limited("update", 10, 3600)
        credentials, options = validated_input(request.get_json())
        old = store.decrypt(store.get(owner)["credentials"])
        if (credentials["username"], credentials["kind"]) != (old["username"], old["kind"]):
            raise ValueError("更新凭据时请使用原学号及学生类型；切换账号请新建订阅")
        store.update(owner, credentials, options)
        return jsonify(state())

    @app.post("/api/sources")
    def save_source():
        owner = require_owner()
        limited("source-edit", 30, 3600)
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - {"id", "kind", "name", "content", "url"}:
            raise ValueError("请提供 ICS 文件或链接")
        kind, name, identity = data.get("kind"), data.get("name"), data.get("id")
        if (not isinstance(kind, str) or kind not in {"upload", "url"} or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or any(ord(c) < 32 for c in name)
                or identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", identity))):
            raise ValueError("请填写来源名称（1–80 字）并选择文件或链接")
        content, url = None, None
        if kind == "upload":
            if "url" in data:
                raise ValueError("文件来源不能同时提供链接")
            content = normalize_ics(data.get("content"))
        else:
            if "content" in data:
                raise ValueError("链接来源不能同时上传文件")
            url = validate_url(data.get("url"))
        try:
            store.save_source(owner, source_id=identity, kind=kind, name=name.strip(), content=content, url=url)
        except LookupError:
            abort(404)
        return jsonify(state())

    @app.post("/api/sources/<source_id>/refresh")
    def refresh_source(source_id):
        owner = require_owner()
        limited("source-refresh", 30, 3600)
        try:
            queued = store.refresh_source(owner, source_id)
        except LookupError:
            abort(404)
        if not queued:
            abort(429, description="每个来源每分钟最多刷新一次")
        return jsonify(state())

    @app.post("/api/sources/<source_id>/delete")
    def delete_source(source_id):
        if not store.delete_source(require_owner(), source_id):
            abort(404)
        return jsonify(state())

    @app.post("/api/refresh")
    def refresh():
        owner = require_owner()
        if store.decrypt(store.get(owner)["credentials"])["mode"] == "snapshot":
            raise ValueError("本机采集订阅没有学校登录会话，请在学校页面重新采集并上传")
        if not store.refresh(owner):
            abort(429, description="每分钟最多手动刷新一次")
        return jsonify(queued=True)

    @app.post("/api/rotate")
    def rotate():
        owner = require_owner()
        store.rotate(owner)
        return jsonify(state())

    @app.post("/api/management-key")
    def management_key():
        owner = require_owner()
        key = store.rotate(owner, management=True)
        attach_session(owner)
        return jsonify(**state(), management_key=key)

    @app.post("/api/logout")
    def logout():
        store.drop_session(request.cookies.get(cookie_name, ""))
        g.clear_cookie = True
        return jsonify(ok=True)

    @app.post("/api/delete")
    def delete():
        store.delete(require_owner())
        g.clear_cookie = True
        return jsonify(ok=True)

    @app.route("/calendar/<token>.ics", methods=["GET", "HEAD"])
    def calendar(token):
        # Missing, revoked and unknown keys all return 404; no upstream request on this path.
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            abort(404)
        row = store.feed(token)
        if row is None:
            abort(404)
        content = store.merged_calendar(row)
        if content is None:
            response = jsonify(error="首次同步尚未成功，请在管理页面查看状态")
            response.status_code = 503
            response.headers["Retry-After"] = "300"
            return response
        response = Response(content, content_type="text/calendar; charset=utf-8")
        response.set_etag(digest(content))
        response.headers["Content-Disposition"] = 'inline; filename="zjuical.ics"'
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.make_conditional(request)
        return response

    # Do not permit attacker-controlled hosts to influence generated links or redirects.
    app.config["TRUSTED_HOSTS"] = [urlsplit(settings.public_url).hostname, "127.0.0.1", "localhost"]
    return app
