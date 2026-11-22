"""Render the koala3 preproduction Compose and Axiops endpoint declarations.

The output is JSON-formatted YAML, accepted by Compose and deploy-manifests CI.
No runtime secrets or local test data are read by this command.
"""

import argparse
import copy
import json
import re
from pathlib import Path

DOMAIN = "ical.koala-studio.org.cn"
PROJECT = "zjuical-preproduction"
DATA = f"/data/services/{PROJECT}/data"
KEY = f"/etc/koala-stateful/{PROJECT}/zjuical.key"
ADDRESS = "172.26.20.3"
PORT = 15273
REPOSITORY = "registry.koala-studio.org.cn/depart-software/deploy-manifests/zjuical"


def manifests(image):
    if not re.fullmatch(re.escape(REPOSITORY) + r"@sha256:[a-f0-9]{64}", image):
        raise ValueError("需要部署仓库中包含 sha256 摘要的 zjuical 镜像")
    common = {
        "image": image,
        "pull_policy": "missing",
        "restart": "unless-stopped",
        "user": "10001:10001",
        "read_only": True,
        "tmpfs": ["/tmp:size=32m,mode=1777"],
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 64,
        "mem_limit": "384m",
        "environment": {
            "PUBLIC_URL": f"https://{DOMAIN}",
            "COOKIE_SECURE": "true",
            "SYNC_INTERVAL": "3600",
            "SYNC_TIMEOUT": "180",
            "MAX_SUBSCRIPTIONS": "1000",
            # Host publication is on the business VLAN; do not trust client XFF.
            "TRUSTED_PROXY_HOPS": "0",
            "ZJUICAL_KEY_FILE": "/run/secrets/zjuical_key",
            "ZJUICAL_DATA_DIR": "/data",
            "TZ": "Asia/Shanghai",
        },
        "volumes": [
            {"type": "bind", "source": DATA, "target": "/data",
             "bind": {"create_host_path": False}},
            {"type": "bind", "source": KEY, "target": "/run/secrets/zjuical_key",
             "read_only": True, "bind": {"create_host_path": False}},
        ],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }
    inputs = {
        DATA: {"type": "private-directory", "uid": 10001, "gid": 10001, "mode": "0700"},
        KEY: {"type": "private-file", "target": "/run/secrets/zjuical_key",
              "uid": 10001, "gid": 10001, "mode": "0400"},
    }
    result = {}
    for role in ("web", "worker"):
        name = f"zjuical-{role}"
        service = copy.deepcopy(common)
        if role == "web":
            service["ports"] = [{"target": 5273, "published": str(PORT),
                                 "host_ip": ADDRESS, "protocol": "tcp"}]
            service["healthcheck"] = {
                "test": ["CMD", "python", "-c",
                         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5273/healthz', timeout=5)"],
                "interval": "30s", "timeout": "10s", "start_period": "10s", "retries": 3,
            }
        else:
            service["command"] = ["python", "-m", "server.worker"]
        result[f"stateful/koala3/{name}-preproduction.yaml"] = {
            "name": PROJECT,
            "x-required-inputs": inputs,
            "services": {name: service},
            "networks": {"default": {"name": f"{PROJECT}_default", "driver": "bridge"}},
        }
    result["software-department/infrastructure/preproduction/external-zjuical.yaml"] = {
        "services": {"external-zjuical": {
            "image": "axiops.io-vii.com/resource:1",
            "x-axiops-outputs": {
                "httpEndpoint": {"render": f"http://{ADDRESS}:{PORT}", "sensitive": False},
                "route": {
                    "render": {"host": DOMAIN,
                               "endpoint": {"host": ADDRESS, "port": PORT, "protocol": "http"}},
                    "sensitive": False,
                    "contributions": [{"resource": "traefik", "input": "routes", "key": PROJECT}],
                },
            },
        }},
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        documents = manifests(args.image)
    except ValueError as error:
        parser.error(str(error))
    # Refuse to partially replace a previously rendered deployment.
    paths = [args.output / relative for relative in documents]
    if any(path.exists() for path in paths):
        parser.error("输出文件已存在，请使用新的输出目录")
    for relative, document in documents.items():
        path = args.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
        print(path)


if __name__ == "__main__":
    main()
