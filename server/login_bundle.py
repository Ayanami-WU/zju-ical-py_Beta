"""Downloadable local helper source, selected from public source paths only."""

import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = """ZJU Calendar 本机登录助手（Python 3.12+）

在解压目录打开本机终端，依次执行：
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m server.local_login

Windows 可使用 .venv\\Scripts\\python.exe 代替 .venv/bin/python。
研究生在最后一个命令后添加 --kind grs。

只在本机交互终端输入账号密码，密码不会回显、不会写入文件，也不会
发送给课表服务器。登录由短时子进程完成，最多 75 秒，不自动重试。
可先审阅 server/local_login.py 和 server/school_login.py。

成功后，在课表网页导入 teaching-session.local.json，检查并提交。
文件仅含教务专用会话，仍具有教务系统访问权限，请保管并在导入后删除。
学校要求验证码/二次认证或接口变化时，请改用网页中的官方登录方式。
本机进程仍会短暂持有密码，无法防护已被控制的电脑或恶意软件。

源码与许可证：https://github.com/Ayanami-WU/zju-ical-py_Beta
本工具继承原项目 LGPL-2.1，参见 LICENSE；保留各文件原作者声明。
"""


def local_login_bundle(root=ROOT):
    paths = [root / "server" / name for name in
             ["__init__.py", "local_login.py", "school_login.py"]]
    for folder in ["course", "exam", "ical", "todos", "utils", "zjuam"]:
        paths.extend(sorted((root / folder).rglob("*.py")))
    paths.extend([root / "configs/config.json", root / "LICENSE"])
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("本机助手包不能包含符号链接或外部文件")
            bundle.writestr(path.relative_to(root).as_posix(), path.read_bytes())
        bundle.writestr("requirements.txt", (root / "requirements-login.txt").read_bytes())
        bundle.writestr("使用说明.txt", README)
    return result.getvalue()
