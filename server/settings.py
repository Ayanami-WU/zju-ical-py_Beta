import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_url: str
    encryption_key: bytes
    cookie_secure: bool = True
    interval: int = 3600
    job_timeout: int = 180
    session_ttl: int = 86400 * 7
    max_subscriptions: int = 1000
    proxy_hops: int = 0

    def __post_init__(self):
        parsed = urlsplit(self.public_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_URL 必须是无路径、无凭据的 HTTP(S) 地址")
        if not self.cookie_secure and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("仅本机开发可关闭 Secure Cookie")
        if self.cookie_secure and parsed.scheme != "https":
            raise ValueError("公网服务必须使用 HTTPS PUBLIC_URL")
        if self.interval < 900 or not 30 <= self.job_timeout <= 600:
            raise ValueError("刷新间隔至少 900 秒，任务超时须为 30–600 秒")
        if not 0 <= self.proxy_hops <= 3 or not 1 <= self.max_subscriptions <= 100000:
            raise ValueError("受信代理跳数或订阅数量上限无效")
        Fernet(self.encryption_key)

    @classmethod
    def from_env(cls):
        key_file = os.environ.get("ZJUICAL_KEY_FILE", "/run/secrets/zjuical_key")
        return cls(
            data_dir=Path(os.environ.get("ZJUICAL_DATA_DIR", "/data")),
            public_url=os.environ.get("PUBLIC_URL", "https://calendar.example.com").rstrip("/"),
            encryption_key=Path(key_file).read_bytes().strip(),
            cookie_secure=os.environ.get("COOKIE_SECURE", "true").lower() != "false",
            interval=int(os.environ.get("SYNC_INTERVAL", "3600")),
            job_timeout=int(os.environ.get("SYNC_TIMEOUT", "180")),
            max_subscriptions=int(os.environ.get("MAX_SUBSCRIPTIONS", "1000")),
            proxy_hops=int(os.environ.get("TRUSTED_PROXY_HOPS", "0")),
        )
