"""Deployment boundaries which would otherwise leak credentials or lose data."""

import os
import tempfile
import unittest
from pathlib import Path

from tools.init_external import initialize
from tools.render_external import ADDRESS, DATA, KEY, PORT, REPOSITORY, manifests


class ExternalDeploymentTests(unittest.TestCase):
    def test_initialization_preserves_key_and_rejects_key_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, key = root / "data", root / "private" / "zjuical.key"
            initialize(data, key, os.getuid(), os.getgid())
            original = key.read_bytes()
            (data / "subscriptions.sqlite3").write_bytes(b"existing database")
            initialize(data, key, os.getuid(), os.getgid())
            self.assertEqual(key.read_bytes(), original)
            self.assertEqual(key.stat().st_mode & 0o777, 0o400)
            key.unlink()
            with self.assertRaisesRegex(ValueError, "已有数据"):
                initialize(data, key, os.getuid(), os.getgid())
            self.assertFalse(key.exists())

    def test_mutable_or_unrelated_image_is_rejected(self):
        for image in [REPOSITORY + ":latest", "unrelated/image@sha256:" + "a" * 64]:
            with self.subTest(image=image), self.assertRaises(ValueError):
                manifests(image)

    def test_shared_persistence_and_key_are_required_without_exposing_worker(self):
        documents = manifests(REPOSITORY + "@sha256:" + "a" * 64)
        services = [next(iter(value["services"].values())) for path, value in documents.items()
                    if path.startswith("stateful/")]
        self.assertEqual(len(services), 2)
        self.assertEqual(services[0]["volumes"], services[1]["volumes"])
        for service in services:
            mounts = {mount["source"]: mount for mount in service["volumes"]}
            self.assertEqual(set(mounts), {DATA, KEY})
            self.assertTrue(mounts[KEY]["read_only"])
            self.assertTrue(all(not mount["bind"]["create_host_path"] for mount in mounts.values()))
            self.assertEqual(service["environment"]["COOKIE_SECURE"], "true")
            self.assertEqual(service["environment"]["TRUSTED_PROXY_HOPS"], "0")
            self.assertNotIn("depends_on", service)
        self.assertEqual(services[0]["ports"][0]["host_ip"], ADDRESS)
        self.assertNotIn("ports", services[1])
        resource = next(value for path, value in documents.items() if path.startswith("software-"))
        external = resource["services"]["external-zjuical"]
        self.assertNotIn("volumes", external)
        route = external["x-axiops-outputs"]["route"]
        self.assertEqual(route["render"]["endpoint"]["port"], PORT)
        self.assertEqual(route["contributions"][0]["resource"], "traefik")


if __name__ == "__main__":
    unittest.main()
