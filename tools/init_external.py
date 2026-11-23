"""Initialize empty external-service storage as root; never replace a key.

Run on koala3, or inside the service image with only its two host directories
mounted. This script uses the standard library and never displays key material.
"""

import base64
import os
import secrets
from pathlib import Path

DATA = Path("/data/services/zjuical-preproduction/data")
KEY = Path("/etc/koala-stateful/zjuical-preproduction/zjuical.key")


def initialize(data=DATA, key=KEY, uid=10001, gid=10001):
    if data.is_symlink() or key.is_symlink():
        raise ValueError("数据目录和密钥不能是符号链接")
    # Refuse to invent a replacement key for a pre-existing database/backup.
    if data.exists() and any(data.iterdir()) and not key.exists():
        raise ValueError("已有数据但缺少密钥；请恢复匹配的原密钥")
    if key.exists():
        raw = base64.b64decode(key.read_bytes().strip(), altchars=b"-_", validate=True)
        if len(raw) != 32:
            raise ValueError("已有密钥格式无效")
    data.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.mkdir(exist_ok=True, mode=0o700)
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(data, uid, gid)
    os.chmod(data, 0o700)
    if not key.exists():
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        with os.fdopen(descriptor, "wb") as output:
            output.write(base64.urlsafe_b64encode(secrets.token_bytes(32)) + b"\n")
    os.chown(key, uid, gid)
    os.chmod(key, 0o400)


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("请在目标主机以 root 或初始化容器运行")
    os.umask(0o077)
    initialize()
    print("预生产目录已准备；已有密钥保持不变，请单独备份。")
