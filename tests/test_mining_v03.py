import csv
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import fitz

from geobim_sheet.agent import LLMConfig
from geobim_sheet.app import create_app
from geobim_sheet.tools import (
    DEMO_REVIEW_DIR,
    ReviewWorkspace,
    check_utility_proximity,
    compare_revisions,
    export_review_package,
    georeference_project,
    list_layers,
    open_project,
)


MINING_PROMPT = "对比这两版选矿厂设计，找出位置变动的设备。检查渣浆泵房附近全部管线与边坡需要复核的项目，生成一张前后对照图和一份问题清单。"


class MiningV03Tests(unittest.TestCase):
    def test_mineworks_cgcs2000_clearance_rules_and_exports(self):
        with TemporaryDirectory() as tmp:
            ws = ReviewWorkspace(tmp)
            opened = open_project(preset="mineworks", workspace=ws)
            self.assertEqual(opened["status"], "ok")
            self.assertEqual(opened["crs"], "EPSG:4547")
            pid = opened["project_id"]

            layers = list_layers(pid, workspace=ws)
            layer_names = {item["layer"] for item in layers["layers"]}
            self.assertTrue({"water", "slurry", "power", "drainage", "slope"}.issubset(layer_names))

            cmp_res = compare_revisions(pid, workspace=ws)
            moved = {m["tag"]: m for m in cmp_res["moved_assets"]}
            self.assertIn("SP-101", moved)
            self.assertGreater(moved["SP-101"]["displacement_m"], 6.0)

            prox = check_utility_proximity(pid, asset_query="渣浆泵房", layer="all", buffer_m=30.0, workspace=ws)
            self.assertEqual(prox["status"], "ok")
            self.assertEqual(prox["asset"]["tag"], "SP-101")
            self.assertGreaterEqual(prox["violation_count"], 1)
            self.assertGreaterEqual(prox["missing_elevation_count"], 2)
            by_id = {i["utility_id"]: i for i in prox["items"]}
            self.assertIn("SL-01", by_id)
            self.assertEqual(by_id["SL-01"]["rule_code"], "MINE-CLR-SLURRY-01")
            self.assertEqual(by_id["SL-01"]["elevation_note"], "待核实高程")
            self.assertIn("SLP-01", by_id)
            self.assertEqual(by_id["SLP-01"]["rule_code"], "MINE-CLR-SLOPE-01")

            exported = export_review_package(pid, focus_asset_tag="SP-101", utility_layer="all", buffer_m=30.0, workspace=ws)
            self.assertEqual(exported["status"], "ok")
            by_kind = {f["kind"]: f for f in exported["files"]}
            with fitz.open(by_kind["pdf"]["path"]) as doc:
                text = doc[0].get_text()
                self.assertIn("SP-101", text)
                self.assertIn("EPSG:4547", text)
                self.assertIn("待核实高程", text)

            with Path(by_kind["csv"]["path"]).open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertTrue(any(r["rule_code"] == "MINE-CLR-SLURRY-01" for r in rows))
            self.assertTrue(any(r["compliance_status"] == "violation" for r in rows))
            self.assertTrue(any(r["elevation_note"] == "待核实高程" for r in rows))

    def test_mineworks_direct_dxf_utility_ingestion(self):
        with TemporaryDirectory() as tmp:
            ws = ReviewWorkspace(tmp)
            opened = open_project(preset="mineworks_dxf", workspace=ws)
            self.assertEqual(opened["status"], "ok")
            self.assertEqual(opened["gis"]["format"], "dxf")
            self.assertEqual(opened["crs"], "EPSG:4547")
            pid = opened["project_id"]

            prox = check_utility_proximity(pid, asset_tag="SP-101", layer="all", buffer_m=30.0, workspace=ws)
            self.assertEqual(prox["status"], "ok")
            self.assertGreaterEqual(prox["review_count"], 4)
            by_id = {i["utility_id"]: i for i in prox["items"]}
            self.assertIn("SL-01", by_id)
            self.assertEqual(by_id["SL-01"]["elevation_note"], "待核实高程")
            self.assertIn("WL-01", by_id)
            self.assertAlmostEqual(by_id["WL-01"]["bury_depth_m"], 1.9, places=2)

    def test_explicit_georeference_project_unlocks_local_ifc(self):
        with TemporaryDirectory() as tmp:
            ws = ReviewWorkspace(tmp)
            opened = open_project(
                rev_a_path=str(DEMO_REVIEW_DIR / "mineworks-rev-a.ifc"),
                rev_b_path=str(DEMO_REVIEW_DIR / "mineworks-rev-b-local.ifc"),
                gis_path=str(DEMO_REVIEW_DIR / "mineworks-utilities.dxf"),
                workspace=ws,
            )
            self.assertEqual(opened["status"], "incomplete_coordinates")
            pid = opened["project_id"]

            fixed = georeference_project(
                project_id=pid,
                crs="EPSG:4547",
                eastings=518400.0,
                northings=3426800.0,
                workspace=ws,
            )
            self.assertEqual(fixed["status"], "ok")
            self.assertEqual(fixed["crs"], "EPSG:4547")
            self.assertEqual(fixed["georeference_source"], "user_override")

            cmp_res = compare_revisions(pid, workspace=ws)
            moved = {m["tag"]: m for m in cmp_res["moved_assets"]}
            self.assertIn("SP-101", moved)
            self.assertAlmostEqual(moved["SP-101"]["displacement_m"], 6.80, places=1)

    def test_mineworks_dup_and_web_dxf_upload_with_crs_override(self):
        with TemporaryDirectory() as tmp:
            app = create_app(data_dir=tmp, llm_config=LLMConfig("demo", "", "deterministic-demo-planner", ""))
            client = app.test_client()

            # 1. Duplicate slurry pump houses pause & resume
            r1 = client.post("/api/agent/tasks", json={"scenario": "mineworks_dup", "instruction": MINING_PROMPT})
            self.assertEqual(r1.status_code, 201, r1.json)
            self.assertEqual(r1.json["status"], "waiting_for_user")
            self.assertEqual({c["tag"] for c in r1.json["pending_question"]["candidates"]}, {"SP-101", "SP-102"})

            r2 = client.post(f"/api/agent/tasks/{r1.json['task_id']}/reply", json={"selected_tag": "SP-101"})
            self.assertEqual(r2.status_code, 200, r2.json)
            self.assertEqual(r2.json["status"], "completed")
            self.assertEqual({a["kind"] for a in r2.json["artifacts"]}, {"pdf", "csv", "dxf"})

            # 2. Multipart upload with local un-georeferenced IFC + DXF utility + explicit CGCS2000 override
            r3 = client.post(
                "/api/agent/tasks",
                data={
                    "instruction": MINING_PROMPT,
                    "override_crs": "EPSG:4547",
                    "override_eastings": "518400",
                    "override_northings": "3426800",
                    "rev_a": (io.BytesIO((DEMO_REVIEW_DIR / "mineworks-rev-a.ifc").read_bytes()), "mine-r1.ifc"),
                    "rev_b": (io.BytesIO((DEMO_REVIEW_DIR / "mineworks-rev-b-local.ifc").read_bytes()), "mine-r2.ifc"),
                    "gis": (io.BytesIO((DEMO_REVIEW_DIR / "mineworks-utilities.dxf").read_bytes()), "mine-pipes.dxf"),
                },
            )
            self.assertEqual(r3.status_code, 201, r3.json)
            self.assertEqual(r3.json["status"], "completed")
            self.assertEqual({a["kind"] for a in r3.json["artifacts"]}, {"pdf", "csv", "dxf"})


if __name__ == "__main__":
    unittest.main()
