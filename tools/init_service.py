"""Generate the Docker service encryption key without displaying its value."""

import base64
import os
import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / ".secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "zjuical.key"
    if not path.exists():
        # Compose file secrets retain host file permissions. The parent directory is
        # private; this key alone is readable by the non-root container user.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as file:
            file.write(base64.urlsafe_b64encode(secrets.token_bytes(32)) + b"\n")
        os.chmod(path, 0o644)
        print("已创建 .secrets/zjuical.key；请单独备份该文件，不要提交到 Git。")
    else:
        print("已保留现有加密密钥。")
    env = root / ".env"
    if not env.exists():
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write((root / ".env.example").read_text())
        print("已创建 .env，请填写 PUBLIC_URL 和 SITE_ADDRESS 后启动。")


if __name__ == "__main__":
    main()
