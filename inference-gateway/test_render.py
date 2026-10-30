"""Rejection paths of render.py: python3 -m unittest discover -s inference-gateway"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render

SRC = Path(__file__).resolve().parent
GOOD_ENTRY = '  - { name: fullsend-ai/org, id: "1434056985", owner_id: "271249281" }\n'


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for f in ("allowlist.yaml", "gateway.env", "config.tmpl.yaml"):
            shutil.copy(SRC / f, self.dir / f)
        self.addCleanup(shutil.rmtree, self.dir)
        patcher = mock.patch.object(render, "HERE", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        (self.dir / name).write_text(text)

    def append(self, name, text):
        with (self.dir / name).open("a") as f:
            f.write(text)

    def assertRejected(self, fn, fragment):
        with self.assertRaises(ValueError) as cm:
            fn()
        self.assertIn(fragment, str(cm.exception))

    def test_committed_inputs_render_and_pass_checks(self):
        text = render.render()
        self.assertEqual(render.check_config(text), [])
        self.assertIn('"fullsend-ai/fullsend@1176916808@271249281"', text)

    def test_cel_injection_in_name(self):
        self.append(
            "allowlist.yaml", '  - { name: \'x"] || true || ["\', id: "1", owner_id: "2" }\n'
        )
        self.assertRejected(render.render, "is not an owner/name repository")

    def test_unquoted_id(self):
        self.write("allowlist.yaml", "repos:\n  - { name: a/b, id: 1, owner_id: '2' }\n")
        self.assertRejected(render.render, "must be a quoted digit string")

    def test_duplicate_entry(self):
        self.write("allowlist.yaml", "repos:\n" + GOOD_ENTRY + GOOD_ENTRY)
        self.assertRejected(render.render, "duplicate entry")

    def test_extra_key(self):
        self.write("allowlist.yaml", "repos:\n  - { name: a/b, id: '1', owner_id: '2', ref: x }\n")
        self.assertRejected(render.render, "exactly name, id, owner_id")

    def test_env_missing_duplicate_and_shell(self):
        env = (self.dir / "gateway.env").read_text()
        lines = {line.split("=", 1)[0]: line for line in env.splitlines() if "=" in line}
        for key in render.INT_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, lines, f"{key} missing from the committed gateway.env")
                self.write("gateway.env", env.replace(lines[key] + "\n", ""))
                self.assertRejected(render.load_env, f"missing ['{key}']")
                self.write("gateway.env", env.replace(lines[key], f"{key}=$(id)"))
                self.assertRejected(render.load_env, "plain value")
                self.write("gateway.env", env.replace(lines[key], f"{key}=0"))
                self.assertRejected(render.load_env, "positive integer")
        self.write("gateway.env", env.replace(lines["CONCURRENCY"], "CONCURRENCY=1001"))
        self.assertRejected(render.load_env, "at most 1000")
        self.write("gateway.env", env + "DIGEST=" + "0" * 64 + "\n")
        self.assertRejected(render.load_env, "duplicate DIGEST")
        self.write("gateway.env", env + "EXTRA=1\n")
        self.assertRejected(render.load_env, "expected KEY=VALUE")

    def test_unknown_marker(self):
        self.append("config.tmpl.yaml", "# __NOPE__\n")
        self.assertRejected(render.render, "unknown marker __NOPE__")

    def test_model_without_allowlist_rule(self):
        for rules in (
            "",
            "    authorization:\n      rules:\n      - 'true'\n",
            "    authorization:\n      rules:\n      - deny: 'false'\n",
            "    authorization:\n      rules:\n      - allow: 'true'\n",
            "    authorization:\n      rules:\n      - allow: 'true || "
            + render.CALLER.replace("'", "''")
            + " in []'\n",
        ):
            with self.subTest(rules=rules):
                shutil.copy(SRC / "config.tmpl.yaml", self.dir / "config.tmpl.yaml")
                self.append("config.tmpl.yaml", "  - name: open\n    provider: openAI\n" + rules)
                errors = render.check_config(render.render())
                self.assertEqual(len(errors), 1)
                self.assertIn("'open'", errors[0])

    def test_jwt_auth_must_stay_strict_github(self):
        for old, new in (
            ("mode: strict", "mode: permissive"),
            ('audiences: ["__AUD__"]', 'audiences: ["other"]'),
            ("issuer: https://token.actions.githubusercontent.com", "issuer: https://evil.example"),
            (
                "jwks: { url: https://token.actions.githubusercontent.com/.well-known/jwks }",
                "jwks: { url: https://evil.example/jwks }",
            ),
        ):
            with self.subTest(change=new):
                tmpl = (SRC / "config.tmpl.yaml").read_text()
                self.assertIn(old, tmpl)
                self.write("config.tmpl.yaml", tmpl.replace(old, new))
                errors = render.check_config(render.render())
                self.assertTrue(any("jwtAuth" in e for e in errors), errors)

    def test_extra_sections_and_rate_keys(self):
        tmpl = (SRC / "config.tmpl.yaml").read_text()
        self.write("config.tmpl.yaml", tmpl + "binds: []\n")
        self.assertIn("top level", render.check_config(render.render())[0])
        self.write(
            "config.tmpl.yaml", tmpl.replace('? "fleet" : "other"', '? "other" : "other"', 1)
        )
        errors = render.check_config(render.render())
        self.assertTrue(any("localRateLimit" in e for e in errors), errors)

    def test_check_flags_stale_config(self):
        (self.dir / "config.yaml").write_text(render.render())
        render.main(["--check"])
        self.append("allowlist.yaml", '  - { name: a/b, id: "9", owner_id: "9" }\n')
        with self.assertRaises(SystemExit) as cm:
            render.main(["--check"])
        self.assertIn("stale", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
