import json
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mps_node_url_switchboard as mph  # noqa: E402

PORT = 63399
HEADERS = {"Host": f"127.0.0.1:{PORT}"}
POST_HEADERS = {**HEADERS, "Origin": f"http://127.0.0.1:{PORT}"}
TARGET = "/node?ref=r%3Aabc%28m%29%2F123"

PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:F798 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 111 1 0 100 0 0 10 0
   1: 0100007F:F7A2 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 222 1 0 100 0 0 10 0
   2: 0100007F:C350 0100007F:F798 01 00000000:00000000 00:00000000 00000000  1000        0 333 1 0 100 0 0 10 0
"""

RECENT_PROJECTS = """\
<application>
  <component name="RecentProjectsManager">
    <option name="additionalInfo">
      <map>
        <entry key="$USER_HOME$/git/a/mpsModules">
          <value><RecentProjectMetaInfo frameTitle="project-a – Some.mps" opened="true" /></value>
        </entry>
        <entry key="$USER_HOME$/git/b/closed">
          <value><RecentProjectMetaInfo frameTitle="project-b" /></value>
        </entry>
        <entry key="$USER_HOME$/git/c/untitled">
          <value><RecentProjectMetaInfo opened="true" /></value>
        </entry>
      </map>
    </option>
  </component>
</application>
"""


class ParsingTest(unittest.TestCase):
    def test_proc_net_tcp_listen_only(self):
        self.assertEqual(mph.parse_proc_net_tcp(PROC_NET_TCP), {111: 63384, 222: 63394})

    def test_recent_projects_opened_only(self):
        self.assertEqual(mph.parse_recent_projects(RECENT_PROJECTS), ["project-a", "untitled"])

    def test_recent_projects_invalid_xml(self):
        self.assertEqual(mph.parse_recent_projects("<broken"), [])

    def test_wmctrl_windows(self):
        out = "0x01a00025  0 4711 host proj – Some.java\n"
        self.assertEqual(mph.parse_wmctrl_windows(out), [("0x01a00025", 4711, "proj – Some.java")])

    def test_wmctrl(self):
        out = "0x00c00053  0 4711 host proj – Some.java\n0x00c00054  0 4711 host Settings\nbad line\n"
        self.assertEqual(mph.parse_wmctrl(out), {4711: ["proj", "Settings"]})

    def test_properties(self):
        text = "# comment\nidea.config.path=/x/config\n  key : value with spaces \n! other\nnovalue\n"
        self.assertEqual(mph.parse_properties(text), {"idea.config.path": "/x/config", "key": "value with spaces"})

    def test_config_dir_resolution(self):
        home = Path("/home/u")
        self.assertEqual(
            mph.resolve_config_dir(["-Didea.config.path=${user.home}/cfg"], home), Path("/home/u/cfg")
        )
        self.assertEqual(
            mph.resolve_config_dir(["-Didea.paths.selector=MPS2025.1"], home),
            Path("/home/u/.config/JetBrains/MPS2025.1"),
        )
        self.assertIsNone(mph.resolve_config_dir(["java"], home))
        with tempfile.TemporaryDirectory() as tmp:
            props = Path(tmp) / "idea.properties"
            props.write_text("idea.config.path=/from/file\n")
            argv = [f"-Didea.properties.file={props}", "-Didea.paths.selector=MPS2025.1"]
            self.assertEqual(mph.resolve_config_dir(argv, home), Path("/from/file"))

    def test_find_mps_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "MPS-2026.1"
            (install / "bin").mkdir(parents=True)
            for name in ("mps", "fsnotifier"):
                (install / "bin" / name).touch()
            (install / "product-info.json").write_text(json.dumps({
                "productCode": "MPS", "envVarBaseName": "MPS", "dataDirectoryName": "MPS2026.1",
                "launch": [{"os": "Linux", "launcherPath": "bin/mps"}],
            }))
            mpsconfig = root / ".mpsconfig" / "MPS-2026-1-p"
            mpsconfig.mkdir(parents=True)
            (mpsconfig / "mps64.vmoptions").write_text("-Xmx4g\n-Dide.httpsupport.internalPort=63322\n")
            (mpsconfig / "idea.properties").write_text(f"idea.config.path={mpsconfig / 'config'}\n")
            proc = root / "proc"

            def add(pid, argv, exe=None, environ=None):
                (proc / pid).mkdir(parents=True)
                (proc / pid / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
                if exe:
                    (proc / pid / "exe").symlink_to(exe)
                if environ is not None:
                    (proc / pid / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environ.items()))

            add("10", ["java", "-Didea.paths.selector=MPS2025.1", mph.MPS_MAIN_CLASS])
            add("20", [str(install / "bin" / "mps")], install / "bin" / "mps", {
                "MPS_VM_OPTIONS": mpsconfig / "mps64.vmoptions", "MPS_PROPERTIES": mpsconfig / "idea.properties",
            })
            add("30", [str(install / "bin" / "mps")], install / "bin" / "mps", {})
            add("40", [str(install / "bin" / "fsnotifier")], install / "bin" / "fsnotifier", {})
            add("50", ["python3", "x.py"], Path(sys.executable))
            (proc / "self").mkdir()

            processes = mph.find_mps_processes(proc)
            self.assertEqual(sorted(processes), [10, 20, 30])
            home = Path("/home/u")
            self.assertEqual(mph.resolve_config_dir(processes[20], home), mpsconfig / "config")
            self.assertIn("-Dide.httpsupport.internalPort=63322", processes[20])
            self.assertNotIn("-Xmx4g", processes[20])
            # No launcher environment -> default config dir of the installation
            self.assertEqual(mph.resolve_config_dir(processes[30], home), home / ".config/JetBrains/MPS2026.1")

    def test_mpsconfig_id(self):
        cfg = Path("/home/u/git/p/.mpsconfig/MPS-2025-1-4-linux-p/config")
        self.assertEqual(mph.mpsconfig_id(cfg), "MPS-2025-1-4-linux-p")
        self.assertIsNone(mph.mpsconfig_id(Path("/home/u/.config/JetBrains/MPS2025.1")))
        self.assertIsNone(mph.mpsconfig_id(None))

    def test_split_error_page(self):
        body = b"<!doctype html><title>500</title><h1>500</h1><p>pkg.Error: No delimiter &amp; more\n\tat x.y(Z.java:1)"
        self.assertEqual(mph.split_error_page(body), ("pkg.Error: No delimiter & more", "\tat x.y(Z.java:1)"))
        self.assertEqual(mph.split_error_page(b""), ("", ""))

    def test_parse_server_header(self):
        self.assertEqual(mph.parse_server_header("JetBrains MPS 2025.1.4"), "MPS 2025.1.4")
        self.assertIsNone(mph.parse_server_header("JetBrains IntelliJ IDEA 2025.1"))
        self.assertIsNone(mph.parse_server_header(""))

    def test_interpret_mps_reply(self):
        head = b"<b>MPS - HTTP Support Plugin</b><br>"
        opened = mph.interpret_mps_reply(
            200, head + b"The requested node has been opened in MPS<br><b>Node</b>: Foo <i>(a.b.Concept)</i><br><b>Project</b>: p1")
        self.assertEqual((opened.outcome, opened.ok), ("opened", True))
        self.assertEqual(opened.lines, ("The requested node has been opened in MPS", "Node: Foo (a.b.Concept)", "Project: p1"))
        missing = mph.interpret_mps_reply(
            200, head + b"The requested node has not been found<br><b>Node reference</b>: r:0(x)/1<br><b>Project</b>: p1")
        self.assertEqual((missing.outcome, missing.ok), ("not_found", False))
        self.assertEqual(missing.lines[1], "Node reference: r:0(x)/1")
        self.assertEqual(mph.interpret_mps_reply(200, head + b"No project is available").outcome, "no_project")
        self.assertEqual(mph.interpret_mps_reply(200, b"something else").outcome, "sent")
        error = mph.interpret_mps_reply(500, b"<h1>500</h1><p>pkg.Error: bad ref\n\tat x")
        self.assertEqual((error.outcome, error.ok, error.lines, error.detail), ("error", False, ("pkg.Error: bad ref",), "\tat x"))
        long_message = "pkg.Error: " + "x" * 500
        self.assertEqual(mph.interpret_mps_reply(500, f"<p>{long_message}".encode()).lines, (long_message,))

    def test_normalize_target(self):
        self.assertEqual(mph.normalize_target(TARGET), TARGET)
        self.assertEqual(mph.normalize_target("http://127.0.0.1:63320/node?ref=x"), "/node?ref=x")
        self.assertIsNone(mph.normalize_target("node"))
        self.assertIsNone(mph.normalize_target("/x\r\nSet-Cookie: a=b"))
        self.assertIsNone(mph.normalize_target("/a b"))


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sub" / "history.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_persisted_upserts(self):
        history = mph.History(self.db)
        history.record_request(TARGET)
        history.record_request(TARGET)
        history.record_forward(TARGET, mph.Instance(1, 63320, "MPS", ("p",)), "p")
        history.record_forward(TARGET, mph.Instance(1, 63320, "MPS", ("p",)), "p")
        row = mph.History(self.db).get(TARGET)
        self.assertEqual((row["hits"], row["forwards"], row["last_port"], row["last_pid"], row["last_project"]),
                         (2, 2, 63320, 1, "p"))

    def test_migrates_old_schema(self):
        import sqlite3
        self.db.parent.mkdir(parents=True)
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE links (target TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
                    "hits INTEGER NOT NULL DEFAULT 0, last_port INTEGER, last_pid INTEGER, last_project TEXT)")
        con.execute("INSERT INTO links VALUES ('/old', 't', 't', 1, NULL, NULL, NULL)")
        con.commit()
        con.close()
        history = mph.History(self.db)
        self.assertIsNone(history.get("/old")["last_version"])
        self.assertEqual(history.get("/old")["forwards"], 0)
        history.record_forward("/old", mph.Instance(1, 2, "MPS 9", ("p",)), "p")
        self.assertEqual(history.get("/old")["last_version"], "MPS 9")

    def test_migrate_legacy_history(self):
        legacy = Path(self.tmp.name) / "mps-port-handler" / "history.db"
        mph.History(legacy).record_request("/kept")
        target = Path(self.tmp.name) / "mps-node-url-switchboard" / "history.db"
        self.assertTrue(mph.migrate_legacy_history(target, legacy))
        self.assertIsNotNone(mph.History(target).get("/kept"))
        self.assertFalse(legacy.parent.exists())
        self.assertFalse(mph.migrate_legacy_history(target, legacy))

    def test_delete_and_clear(self):
        history = mph.History(self.db)
        history.record_request("/a")
        history.record_request("/b")
        history.delete("/a")
        self.assertEqual([r["target"] for r in history.entries()], ["/b"])
        history.clear()
        self.assertEqual(history.entries(), [])


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.instances = [
            mph.Instance(100, 63320, "MPS 2025.1.4", ("alpha", "beta"), "MPS-2025-1-4-linux-alpha"),
            mph.Instance(200, 63400, "MPS 2024.3", ("gamma",)),
        ]
        self.sent = []
        discovery = mph.Discovery(ttl=0, discover=lambda _exclude: list(self.instances))
        self.history = mph.History(Path(self.tmp.name) / "history.db")
        self.focused = []
        self.app = mph.App(PORT, discovery, self.history, send=self.fake_send, focus=self.fake_focus)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_send(self, url):
        self.sent.append(url)
        return mph.MpsReply(200, "opened", ("The requested node has been opened in MPS", "Node: Foo (Concept)", "Project: alpha"))

    def fake_focus(self, pid, project):
        self.focused.append((pid, project))
        return True, "window focused"

    def get(self, path, headers=HEADERS):
        return self.app.handle("GET", path, headers)

    def forward(self, headers=POST_HEADERS, **params):
        resp = self.app.handle("POST", "/__forward", headers, urlencode(params).encode())
        return resp, json.loads(resp.body) if resp.content_type == "application/json" else None

    def test_page_lists_instances_in_order_and_records(self):
        resp = self.get(TARGET)
        self.assertEqual(resp.status, HTTPStatus.OK)
        body = resp.body.decode()
        for name in ("alpha", "beta", "gamma", "MPS-2025-1-4-linux-alpha", "no .mpsconfig"):
            self.assertIn(name, body)
        row = body[body.index('data-project="alpha"'):]
        self.assertLess(row.index(">2025.1.4<"), row.index(">alpha<"))
        self.assertLess(row.index(">alpha<"), row.index("MPS-2025-1-4-linux-alpha"))
        self.assertIn('id="history"', body)
        self.assertIn(f'data-target="{TARGET}"', body)
        self.assertEqual(self.history.get(TARGET)["hits"], 1)
        self.assertEqual(self.sent, [])

    def test_root_page_without_target(self):
        resp = self.get("/")
        self.assertIn(b"No link selected", resp.body)
        self.assertEqual(self.history.entries(), [])

    def test_page_without_instances(self):
        self.instances.clear()
        resp = self.get(TARGET)
        self.assertIn(b"No MPS instance is running", resp.body)
        self.assertIsNotNone(self.history.get(TARGET))

    def test_forward_sends_server_side_and_records(self):
        resp, data = self.forward(target=TARGET, port=63320, project="beta")
        self.assertEqual(resp.status, HTTPStatus.OK)
        self.assertEqual(self.sent, [f"http://127.0.0.1:63320{TARGET}"])
        self.assertTrue(data["ok"])
        self.assertEqual((data["outcome"], data["mps_lines"][1]), ("opened", "Node: Foo (Concept)"))
        self.assertEqual((data["version"], data["project"], data["config_id"]), ("MPS 2025.1.4", "beta", "MPS-2025-1-4-linux-alpha"))
        row = self.history.get(TARGET)
        self.assertEqual((row["last_pid"], row["last_project"]), (100, "beta"))

    def test_forward_reports_mps_errors(self):
        self.app._send = lambda url: mph.MpsReply(500, "error", ("IncorrectNodeIdFormatException: No delimiter",), "\tat x")
        _, data = self.forward(target=TARGET, port=63400)
        self.assertFalse(data["ok"])
        self.assertEqual((data["status"], data["outcome"]), (500, "error"))
        self.assertEqual((data["port"], data["project"]), (63400, "gamma"))
        self.assertIn("No delimiter", data["mps_lines"][0])
        self.assertEqual(data["mps_detail"], "\tat x")

    def test_forward_not_found_is_not_ok(self):
        self.app._send = lambda url: mph.MpsReply(200, "not_found", ("The requested node has not been found", "Node reference: r:x/1"))
        _, data = self.forward(target=TARGET, port=63400)
        self.assertFalse(data["ok"])
        self.assertEqual((data["outcome"], data["mps_lines"][1]), ("not_found", "Node reference: r:x/1"))

    def test_forward_unknown_project_falls_back_to_first(self):
        self.forward(target=TARGET, port=63320, project="nope")
        self.assertEqual(self.history.get(TARGET)["last_project"], "alpha")

    def test_forward_rejects_unknown_port_bad_target_and_cross_origin(self):
        self.assertEqual(self.forward(target=TARGET, port=1234)[0].status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(self.forward(target="http:x", port=63320)[0].status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(self.forward(target="/a\r\nX: y", port=63320)[0].status, HTTPStatus.BAD_REQUEST)
        foreign = {**HEADERS, "Origin": "http://evil.example"}
        self.assertEqual(self.forward(foreign, target=TARGET, port=63320)[0].status, HTTPStatus.FORBIDDEN)
        self.assertEqual(self.sent, [])

    def test_raise_focuses_window_without_forwarding(self):
        resp = self.app.handle("POST", "/__raise", POST_HEADERS, b"port=63320&project=beta")
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])
        self.assertEqual((data["ok"], data["version"]), (True, "MPS 2025.1.4"))
        self.assertEqual(self.focused, [(100, "beta")])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.app.handle("POST", "/__raise", POST_HEADERS, b"port=1").status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(self.app.handle("POST", "/__raise", HEADERS, b"port=63320").status, HTTPStatus.FORBIDDEN)

    def test_clear_link_button_only_with_target(self):
        self.assertIn(b'id="clear-link">', self.get(TARGET).body)
        self.assertIn(b'id="clear-link" hidden>', self.get("/").body)
        self.assertIn(b'<h1><a href="/"', self.get("/").body)

    def test_get_does_not_forward(self):
        self.assertEqual(self.get("/__forward?" + urlencode({"target": TARGET, "port": 63320})).status, HTTPStatus.NOT_FOUND)
        self.assertEqual(self.sent, [])

    def test_reopen_uses_last_instance_while_running(self):
        self.forward(target=TARGET, port=63400, project="gamma")
        _, data = self.forward(target=TARGET, mode="reopen")
        self.assertEqual(data["url"], f"http://127.0.0.1:63400{TARGET}")
        # Same port, different process -> ask for a choice instead of a blind forward
        self.instances[1] = mph.Instance(201, 63400, "MPS 2024.3", ("gamma",))
        _, data = self.forward(target=TARGET, mode="reopen")
        self.assertTrue(data["needs_choice"])
        self.assertEqual(data["target"], TARGET)
        self.assertEqual(len(self.sent), 2)
        # Forwarded only, never opened on the switchboard
        row = self.history.get(TARGET)
        self.assertEqual((row["hits"], row["forwards"]), (0, 2))

    def test_fragments(self):
        self.forward(target=TARGET, port=63400, project="gamma")
        instances = self.get("/__instances?" + urlencode({"target": TARGET})).body.decode()
        self.assertIn('<li class="line last">', instances)
        history = self.get("/__history").body.decode()
        self.assertIn("gamma", history)
        self.assertIn('class="ver-chip">2024.3<', history)
        self.assertIn('class="text-button reopen"', history)

    def test_history_escaped(self):
        self.get("/node?ref=%3Cscript%3E")
        body = self.get("/__history").body.decode()
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)

    def test_internal_routes_and_favicon_not_recorded(self):
        self.assertEqual(self.get("/favicon.ico").status, HTTPStatus.NOT_FOUND)
        self.assertEqual(self.get("/__nope").status, HTTPStatus.NOT_FOUND)
        self.assertEqual(self.history.entries(), [])

    def test_route_armed_only_with_link(self):
        self.assertIn(b'<body class="armed"', self.get(TARGET).body)
        root = self.get("/").body
        self.assertNotIn(b'class="armed"', root)

    def test_page_has_connection_banner(self):
        self.assertIn(b'<div id="connection" role="alert" hidden>', self.get("/").body)

    def test_host_header_checked(self):
        self.assertEqual(self.get("/", {"Host": "evil.example:63399"}).status, HTTPStatus.FORBIDDEN)
        self.assertEqual(self.get("/", {"Host": f"localhost:{PORT}"}).status, HTTPStatus.OK)

    def test_delete_and_clear_require_same_origin(self):
        self.history.record_request("/a")
        self.history.record_request("/b")
        form = b"target=%2Fa"
        self.assertEqual(self.app.handle("POST", "/__history/delete", HEADERS, form).status, HTTPStatus.FORBIDDEN)
        self.assertEqual(self.app.handle("POST", "/__history/delete", POST_HEADERS, form).status, HTTPStatus.NO_CONTENT)
        self.assertIsNone(self.history.get("/a"))
        referer = {**HEADERS, "Referer": f"http://127.0.0.1:{PORT}/"}
        self.assertEqual(self.app.handle("POST", "/__history/clear", referer).status, HTTPStatus.NO_CONTENT)
        self.assertEqual(self.history.entries(), [])

    def test_forward_results_shared_as_activity(self):
        rev, entries = self.history.activity()
        self.assertEqual(entries, [])
        _, data = self.forward(target=TARGET, port=63320)
        self.forward(target=TARGET, port=1234)
        listing = json.loads(self.get("/__activity").body)
        self.assertNotEqual(listing["rev"], rev)
        self.assertEqual([e["ok"] for e in listing["entries"]], [False, True])
        self.assertEqual(listing["entries"][1]["id"], data["id"])
        self.assertEqual(json.loads(self.get(f"/__activity?rev={listing['rev']}").body), {"rev": listing["rev"]})
        # New pages start with the stored activity
        self.assertIn(f'"id": {data["id"]}'.encode(), self.get("/").body)

        form = f"id={data['id']}".encode()
        self.assertEqual(self.app.handle("POST", "/__activity/delete", HEADERS, form).status, HTTPStatus.FORBIDDEN)
        self.assertEqual(self.app.handle("POST", "/__activity/delete", POST_HEADERS, form).status, HTTPStatus.NO_CONTENT)
        self.assertEqual(len(self.history.activity()[1]), 1)
        self.assertEqual(self.app.handle("POST", "/__activity/clear", POST_HEADERS).status, HTTPStatus.NO_CONTENT)
        self.assertEqual(self.history.activity()[1], [])

    def test_activity_limited_and_escaped_in_page(self):
        for i in range(mph.ACTIVITY_LIMIT + 5):
            self.history.add_activity({"ok": False, "message": f"</script><b>{i}"})
        entries = self.history.activity()[1]
        self.assertEqual(len(entries), mph.ACTIVITY_LIMIT)
        self.assertEqual(entries[0]["message"], f"</script><b>{mph.ACTIVITY_LIMIT + 4}")
        self.assertNotIn(b"</script><b>", self.get("/").body)

    def test_history_link_highlights_model_and_selects(self):
        self.history.record_request("/node?ref=r:1234(NewLanguage.structure)/567")
        body = self.get("/__history").body.decode()
        self.assertIn('<tr class="select" tabindex="0"', body)
        self.assertNotIn("Send to another MPS…</button>", body)
        self.assertIn('data-url="http://127.0.0.1:63320/node?ref=r:1234(NewLanguage.structure)/567"', body)
        self.assertIn('<button class="sort" data-sort="seen">Last seen</button>', body)
        self.assertIn('data-sort-hits="1" data-sort-forwards="0"', body)
        self.assertIn('r:1234(<span class="ref-name">NewLanguage.structure</span>)/567', body)
        self.assertEqual(mph._link_html("/x?a=<b>"), "/x?a=&lt;b&gt;")


if __name__ == "__main__":
    unittest.main()
