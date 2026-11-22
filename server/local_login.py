"""Interactive local password login. Never contacts the calendar hosting service."""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERRORS = {
    "interaction_required": "学校要求额外交互或尚未建立教务会话，请改用网页上的官方登录和手动会话方式。",
    "upstream_unavailable": "暂时无法连接学校认证系统，请稍后再试。",
    "login_failed": "登录未完成，请核对账号密码；未自动重试，以免触发学校锁定。",
}


def write_session(path, credentials):
    allowed = {"username", "kind", "mode", "secret", "root_session", "teaching_token", "route"}
    if (set(credentials) - allowed or credentials.get("mode") != "token"
            or credentials.get("kind") not in {"ugrs", "grs"}
            or not credentials.get("secret")):
        raise ValueError("登录进程返回了无效的教务会话")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"format": "zjuical-teaching-session-v1", "credentials": credentials},
                      output, ensure_ascii=False)
            output.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["ugrs", "grs"], default="ugrs", help="本科 ugrs / 研究生 grs")
    parser.add_argument("--output", type=Path, default=Path("teaching-session.local.json"))
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("请在本机交互终端运行；不接受命令行、环境变量或管道中的密码")
    if args.output.exists() or args.output.is_symlink():
        parser.error("输出文件已存在，请移走旧会话文件或用 --output 指定新的文件名")
    print("密码仅发送至浙大 HTTPS 认证端点；不会上传到课表服务器。")
    print("学校若要求验证码或二次认证，请改用官方网页登录。")
    username = input("浙大学号：").strip()
    if not re.fullmatch(r"[0-9]{5,20}", username):
        parser.error("学号格式无效")
    password = getpass.getpass("统一认证密码（不回显）：")
    if not 1 <= len(password) <= 1024:
        parser.error("密码为空或过长")
    try:
        # Process arguments contain no password. No cookie jar is saved by the child.
        result = subprocess.run(
            [sys.executable, "-m", "server.school_login"],
            input=json.dumps({"username": username, "password": password, "kind": args.kind}),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=ROOT, timeout=75, check=True,
        )
        response = json.loads(result.stdout)
        if "credentials" not in response:
            raise ValueError(ERRORS.get(response.get("error"), ERRORS["login_failed"]))
        write_session(args.output, response["credentials"])
    except subprocess.TimeoutExpired:
        raise SystemExit("学校登录超时，未保存密码或会话。") from None
    except (subprocess.SubprocessError, json.JSONDecodeError):
        raise SystemExit(ERRORS["login_failed"]) from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    finally:
        password = None
    print(f"已生成 {args.output}（不含密码或全局 SSO Cookie）。")
    print("在订阅网页选择“导入本机会话文件”，检查学号并创建订阅。")
    print("会话文件仍可访问教务系统，请妥善保管，导入后可自行删除。")


if __name__ == "__main__":
    main()
