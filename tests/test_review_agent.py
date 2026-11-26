import asyncio
import csv
import http.server
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
import fitz  # PyMuPDF
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from geobim_sheet.agent import LLMConfig
from geobim_sheet.app import create_app
from geobim_sheet.demo.make_demo import build_ifc
from geobim_sheet.tools import (
    DEMO_REVIEW_DIR,
    ReviewWorkspace,
    check_utility_proximity,
    compare_revisions,
    export_review_package,
    find_assets,
    list_layers,
    open_project,
)


ENGINEER_PROMPT = "对比这两版设计，找出位置变动的设备。检查泵房附近有哪些给水管线需要复核，生成一张前后对照图和一份问题清单。"


class ReviewToolsTests(unittest.TestCase):
    def test_normal_revision_tools_and_exports(self):
        with TemporaryDirectory() as tmp:
            ws = ReviewWorkspace(tmp)
            opened = open_project(preset="normal", workspace=ws)
            self.assertEqual(opened["status"], "ok")
            self.assertEqual(opened["crs"], "EPSG:32650")
            pid = opened["project_id"]

            layers = list_layers(pid, workspace=ws)
            self.assertEqual(layers["status"], "ok")
            water_layer = next(item for item in layers["layers"] if item["layer"] == "water")
            self.assertGreaterEqual(water_layer["missing_elevation_count"], 2)

            cmp_res = compare_revisions(pid, workspace=ws)
            self.assertEqual(cmp_res["status"], "ok")
            moved_tags = {item["tag"]: item for item in cmp_res["moved_assets"]}
            self.assertIn("P-101", moved_tags)
            self.assertGreater(moved_tags["P-101"]["displacement_m"], 7.0)
            self.assertEqual(moved_tags["P-101"]["match_method"], "global_id")

            prox = check_utility_proximity(pid, asset_query="泵房", layer="water", buffer_m=30.0, workspace=ws)
            self.assertEqual(prox["status"], "ok")
            self.assertEqual(prox["asset"]["tag"], "P-101")
            self.assertGreaterEqual(prox["review_count"], 2)
            self.assertGreaterEqual(prox["missing_elevation_count"], 2)
            missing_items = [i for i in prox["items"] if i["elevation_note"] == "待核实高程"]
            self.assertTrue(missing_items)

            exported = export_review_package(pid, focus_asset_tag="P-101", utility_layer="water", buffer_m=30.0, workspace=ws)
            self.assertEqual(exported["status"], "ok")
            by_kind = {f["kind"]: f for f in exported["files"]}
            self.assertEqual(set(by_kind), {"pdf", "csv", "dxf"})

            # Verify PDF opens cleanly with PyMuPDF and contains traceable text
            pdf_path = Path(by_kind["pdf"]["path"])
            self.assertTrue(pdf_path.is_file())
            with fitz.open(pdf_path) as doc:
                self.assertEqual(len(doc), 1)
                text = doc[0].get_text()
                self.assertIn("P-101", text)
                self.assertIn("EPSG:32650", text)
                self.assertIn("demoworks-rev-a.ifc", text)
                self.assertIn("待核实高程", text)

            # Verify CSV is valid UTF-8-SIG and traceable
            csv_path = Path(by_kind["csv"]["path"])
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 3)
            self.assertTrue(all(r["project_id"] == pid for r in rows))
            self.assertTrue(all(r["rev_a_file"] == "demoworks-rev-a.ifc" for r in rows))
            self.assertTrue(all(r["rev_b_file"] == "demoworks-rev-b.ifc" for r in rows))
            self.assertTrue(all(r["crs"] == "EPSG:32650" for r in rows))
            self.assertTrue(any(r["elevation_note"] == "待核实高程" for r in rows))
            self.assertTrue(any(r["asset_tag"] == "P-101" and r["rev_a_guid"] for r in rows))

            # Verify DXF parses cleanly
            dxf_doc = ezdxf.readfile(by_kind["dxf"]["path"])
            self.assertFalse(dxf_doc.audit().errors)

    def test_id_changed_candidate_and_ambiguous_fallback(self):
        with TemporaryDirectory() as tmp:
            ws = ReviewWorkspace(tmp)
            opened = open_project(preset="id_changed", workspace=ws)
            self.assertEqual(opened["status"], "ok")
            cmp_res = compare_revisions(opened["project_id"], workspace=ws)
            self.assertEqual(cmp_res["id_changed_count"], 1)
            cand = cmp_res["id_changed_candidates"][0]
            self.assertEqual(cand["tag"], "P-101")
            self.assertNotEqual(cand["rev_a_guid"], cand["rev_b_guid"])
            self.assertEqual(cand["match_method"], "tag_and_type_candidate")
            self.assertTrue(cand["needs_confirmation"])
            self.assertIn("P-101", cand["match_basis"])

            # Confirm match explicitly and verify needs_confirmation becomes False
            confirmed = compare_revisions(
                opened["project_id"],
                confirmed_matches=[{"rev_a_guid": cand["rev_a_guid"], "rev_b_guid": cand["rev_b_guid"]}],
                workspace=ws,
            )
            p101 = next(m for m in confirmed["moved_assets"] if m["tag"] == "P-101")
            self.assertEqual(p101["match_method"], "user_confirmed")
            self.assertFalse(p101["needs_confirmation"])

            # Ambiguous case: two untagged pumps with changed GlobalIds -> ambiguous_matches
            amb_a = Path(tmp) / "amb-a.ifc"
            amb_b = Path(tmp) / "amb-b.ifc"
            build_ifc(
                amb_a,
                [("", "Pump unit", -10, -5, 10, 8, 4, 0), ("", "Pump unit", 20, 10, 10, 8, 4, 0)],
                seed_prefix="amb-a",
            )
            build_ifc(
                amb_b,
                [("", "Pump unit", -8, -5, 10, 8, 4, 0), ("", "Pump unit", 22, 10, 10, 8, 4, 0)],
                seed_prefix="amb-b",
            )
            opened_amb = open_project(
                rev_a_path=str(amb_a),
                rev_b_path=str(amb_b),
                gis_path=str(DEMO_REVIEW_DIR / "demoworks-utilities.geojson"),
                workspace=ws,
            )
            cmp_amb = compare_revisions(opened_amb["project_id"], workspace=ws)
            self.assertEqual(cmp_amb["status"], "needs_confirmation")
            self.assertEqual(cmp_amb["ambiguous_count"], 1)
            self.assertEqual(len(cmp_amb["ambiguous_matches"][0]["rev_a_candidates"]), 2)
            self.assertEqual(len(cmp_amb["ambiguous_matches"][0]["rev_b_candidates"]), 2)

    def test_missing_coordinates_reports_exact_missing_fields(self):
        with TemporaryDirectory() as tmp:
            ws = ReviewWorkspace(tmp)
            opened = open_project(preset="missing_crs", workspace=ws)
            self.assertEqual(opened["status"], "incomplete_coordinates")
            fields = {item["field"] for item in opened["missing_info"]}
            self.assertIn("IfcMapConversion", fields)

            # Subsequent export refuses to generate fake files
            exported = export_review_package(opened["project_id"], workspace=ws)
            self.assertEqual(exported["status"], "incomplete_coordinates")
            self.assertEqual(exported["files"], [])


class WebAgentAndMCPTests(unittest.TestCase):
    def test_duplicate_pumps_pause_resume_and_download_web_api(self):
        with TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, llm_config=LLMConfig("demo", "", "deterministic-demo-planner", ""))
            client = app.test_client()

            # Ensure /agent page loads
            page = client.get("/agent")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Revision Review Agent", page.get_data(as_text=True))

            # Start task on duplicate_pumps scenario
            started = client.post(
                "/api/agent/tasks",
                json={"scenario": "duplicate_pumps", "instruction": ENGINEER_PROMPT},
            )
            self.assertEqual(started.status_code, 201, started.json)
            task = started.json
            self.assertEqual(task["status"], "waiting_for_user")
            self.assertEqual(task["artifacts"], [])
            candidates = task["pending_question"]["candidates"]
            self.assertEqual({c["tag"] for c in candidates}, {"P-101", "P-102"})

            # Resume by picking P-102
            resumed = client.post(
                f"/api/agent/tasks/{task['task_id']}/reply",
                json={"selected_tag": "P-102"},
            )
            self.assertEqual(resumed.status_code, 200, resumed.json)
            done = resumed.json
            self.assertEqual(done["status"], "completed")
            self.assertGreaterEqual(len(done["artifacts"]), 2)
            by_kind = {a["kind"]: a for a in done["artifacts"]}
            self.assertIn("P-102", by_kind["pdf"]["filename"])

            # Download verified PDF and CSV
            pdf_dl = client.get(by_kind["pdf"]["download_url"])
            self.assertEqual(pdf_dl.status_code, 200)
            self.assertTrue(pdf_dl.data.startswith(b"%PDF-"))
            csv_dl = client.get(by_kind["csv"]["download_url"])
            self.assertEqual(csv_dl.status_code, 200)
            self.assertIn("P-102", csv_dl.data.decode("utf-8-sig"))

    def test_web_agent_custom_three_file_upload(self):
        with TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, llm_config=LLMConfig("demo", "", "deterministic-demo-planner", ""))
            client = app.test_client()
            resp = client.post(
                "/api/agent/tasks",
                data={
                    "instruction": ENGINEER_PROMPT,
                    "rev_a": (io.BytesIO((DEMO_REVIEW_DIR / "demoworks-rev-a.ifc").read_bytes()), "plant-r1.ifc"),
                    "rev_b": (io.BytesIO((DEMO_REVIEW_DIR / "demoworks-rev-b.ifc").read_bytes()), "plant-r2.ifc"),
                    "gis": (io.BytesIO((DEMO_REVIEW_DIR / "demoworks-utilities.geojson").read_bytes()), "pipes.geojson"),
                },
            )
            self.assertEqual(resp.status_code, 201, resp.json)
            task = resp.json
            self.assertEqual(task["status"], "completed")
            self.assertEqual({a["kind"] for a in task["artifacts"]}, {"pdf", "csv", "dxf"})
            tool_names = [t["name"] for t in task["tool_calls"]]
            self.assertIn("open_project", tool_names)
            self.assertIn("compare_revisions", tool_names)
            self.assertIn("check_utility_proximity", tool_names)
            self.assertIn("export_review_package", tool_names)

    def test_real_openai_compatible_tool_calling_loop_with_mock_http_server(self):
        """Exercises ReviewAgent._run_llm_loop over real HTTP using OpenAI tool-calling wire format."""
        turn_counter = {"n": 0}

        class MockLLMHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                turn_counter["n"] += 1
                step = turn_counter["n"]

                # Extract project_id from prior tool results when present
                project_id = None
                for msg in body.get("messages", []):
                    if msg.get("role") == "tool":
                        try:
                            data = json.loads(msg["content"])
                            if data.get("project_id"):
                                project_id = data["project_id"]
                        except Exception:
                            pass

                if step == 1:
                    reply = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_open",
                                            "type": "function",
                                            "function": {
                                                "name": "open_project",
                                                "arguments": json.dumps({"preset": "duplicate_pumps"}),
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                elif step == 2:
                    reply = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_cmp",
                                            "type": "function",
                                            "function": {
                                                "name": "compare_revisions",
                                                "arguments": json.dumps({"project_id": project_id}),
                                            },
                                        },
                                        {
                                            "id": "call_find",
                                            "type": "function",
                                            "function": {
                                                "name": "find_assets",
                                                "arguments": json.dumps({"project_id": project_id, "query": "泵房"}),
                                            },
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                elif step == 3:
                    # Model sees needs_disambiguation and asks user to choose P-101 or P-102
                    reply = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "检测到两座同名泵房：P-101 与 P-102，请选择其中一座继续。",
                                }
                            }
                        ]
                    }
                elif step == 4:
                    # Resumed after user chose P-101
                    reply = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_prox",
                                            "type": "function",
                                            "function": {
                                                "name": "check_utility_proximity",
                                                "arguments": json.dumps(
                                                    {"project_id": project_id, "asset_tag": "P-101", "layer": "water"}
                                                ),
                                            },
                                        },
                                        {
                                            "id": "call_exp",
                                            "type": "function",
                                            "function": {
                                                "name": "export_review_package",
                                                "arguments": json.dumps(
                                                    {"project_id": project_id, "focus_asset_tag": "P-101"}
                                                ),
                                            },
                                        },
                                    ],
                                }
                            }
                        ]
                    }
                else:
                    reply = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "已完成 P-101 评审并生成前后对照图 PDF 与问题清单 CSV（含待核实高程标记）。",
                                }
                            }
                        ]
                    }

                raw = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockLLMHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                cfg = LLMConfig(
                    provider="openai",
                    base_url=f"http://127.0.0.1:{port}/v1",
                    model="mock-tool-model",
                    api_key="test-key",
                )
                app = create_app(data_dir=tmp, llm_config=cfg)
                client = app.test_client()

                res1 = client.post(
                    "/api/agent/tasks",
                    json={"scenario": "duplicate_pumps", "instruction": ENGINEER_PROMPT},
                )
                self.assertEqual(res1.status_code, 201, res1.json)
                t1 = res1.json
                self.assertEqual(t1["mode"], "llm")
                self.assertEqual(t1["status"], "waiting_for_user")
                self.assertEqual(t1["artifacts"], [])

                res2 = client.post(
                    f"/api/agent/tasks/{t1['task_id']}/reply",
                    json={"selected_tag": "P-101"},
                )
                self.assertEqual(res2.status_code, 200, res2.json)
                t2 = res2.json
                self.assertEqual(t2["status"], "completed")
                self.assertEqual({a["kind"] for a in t2["artifacts"]}, {"pdf", "csv", "dxf"})
        finally:
            server.shutdown()
            server.server_close()

    def test_real_mcp_stdio_end_to_end(self):
        """Spawn the official Python MCP stdio server and run all 6 tools over real stdio."""

        async def run_mcp_flow(tmp_dir: str):
            env = dict(os.environ)
            env["GEOBIM_DATA_DIR"] = tmp_dir
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "geobim_sheet.mcp_server"],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools_resp = await session.list_tools()
                    tool_names = {t.name for t in tools_resp.tools}
                    expected = {
                        "open_project",
                        "find_assets",
                        "list_layers",
                        "compare_revisions",
                        "check_utility_proximity",
                        "export_review_package",
                    }
                    self.assertTrue(expected.issubset(tool_names), tool_names)

                    def parse_call(res):
                        text = "".join(
                            getattr(block, "text", "") for block in res.content
                        )
                        return json.loads(text)

                    opened = parse_call(await session.call_tool("open_project", {"preset": "normal"}))
                    self.assertEqual(opened["status"], "ok")
                    pid = opened["project_id"]

                    layers = parse_call(await session.call_tool("list_layers", {"project_id": pid}))
                    self.assertEqual(layers["status"], "ok")

                    found = parse_call(await session.call_tool("find_assets", {"project_id": pid, "query": "泵房"}))
                    self.assertEqual(found["assets"][0]["tag"], "P-101")

                    cmp_res = parse_call(await session.call_tool("compare_revisions", {"project_id": pid}))
                    self.assertGreaterEqual(cmp_res["moved_count"], 1)

                    prox = parse_call(
                        await session.call_tool(
                            "check_utility_proximity",
                            {"project_id": pid, "asset_tag": "P-101", "layer": "water", "buffer_m": 30.0},
                        )
                    )
                    self.assertGreaterEqual(prox["missing_elevation_count"], 2)

                    exported = parse_call(
                        await session.call_tool(
                            "export_review_package",
                            {"project_id": pid, "focus_asset_tag": "P-101", "utility_layer": "water"},
                        )
                    )
                    self.assertEqual(exported["status"], "ok")
                    for f in exported["files"]:
                        self.assertTrue(Path(f["path"]).is_file(), f)

        with TemporaryDirectory() as tmp:
            asyncio.run(run_mcp_flow(tmp))


if __name__ == "__main__":
    unittest.main()
