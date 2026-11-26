import io
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import ezdxf
import ifcopenshell
from shapely import get_coordinates
from shapely.geometry import shape

from geobim_sheet.app import DEMO_DIR, create_app


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = create_app().test_client()

    def run_prompt(self, prompt, state=None, project="demo"):
        return self.client.post("/api/sheet", json={"project": project, "state": state, "prompt": prompt})

    def test_command_and_chinese_follow_up_clip_geometry(self):
        first = self.run_prompt("P-101, 100 m radius, only roads and water supply")
        self.assertEqual(first.status_code, 200, first.json)
        second = self.run_prompt("只保留给水，范围缩到 60 米", first.json["state"])
        self.assertEqual(second.status_code, 200, second.json)
        result = second.json
        self.assertEqual(result["asset"]["tag"], "P-101")
        self.assertEqual(result["state"]["radius"], 60)
        self.assertEqual(set(result["features"]), {"water"})
        self.assertTrue(result["features"]["water"])
        center_x, center_y = result["center"]
        for feature in result["features"]["water"]:
            for east, north in get_coordinates(shape(feature)):
                self.assertLessEqual(math.hypot(east - center_x, north - center_y), 60.001)
        restored = self.run_prompt("显示全部图层", result["state"])
        self.assertIn("terrain", restored.json["features"])

    def test_exports_use_current_sheet_and_project_coordinates(self):
        result = self.run_prompt("P-101, 60 m radius, only water supply").json
        payload = {"project": "demo", "state": result["state"]}
        pdf = self.client.post("/api/export/pdf", json=payload)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertIn("P-101-60m.pdf", pdf.headers["Content-Disposition"])
        export = self.client.post("/api/export/dxf", json=payload)
        self.assertEqual(export.status_code, 200)
        document = ezdxf.read(io.StringIO(export.data.decode("utf-8")))
        self.assertFalse(document.audit().errors)
        self.assertEqual(document.units, ezdxf.units.M)
        entities = list(document.modelspace())
        self.assertEqual({entity.dxf.layer for entity in entities}, {"WATER", "SUBJECT"})
        label = next(entity for entity in entities if entity.dxftype() == "TEXT")
        self.assertEqual(label.dxf.text, "P-101")
        self.assertAlmostEqual(label.dxf.insert.x, result["center"][0], places=3)
        self.assertAlmostEqual(label.dxf.insert.y, result["center"][1], places=3)

    def test_import_and_reject_an_ifc_without_georeferencing(self):
        model_path = DEMO_DIR / "demoworks-r03.ifc"
        site_path = DEMO_DIR / "demoworks-site-r02.geojson"
        site = site_path.read_bytes()

        def upload(model):
            return self.client.post("/api/projects", data={
                "ifc": (io.BytesIO(model), "design.ifc"), "gis": (io.BytesIO(site), "site.geojson"),
            })

        project = upload(model_path.read_bytes())
        self.assertEqual(project.status_code, 201, project.json)
        self.assertEqual(project.json["ifc"], "design.ifc")
        sheet = self.run_prompt("E-204 周边 60 米", project=project.json["project"])
        self.assertEqual(sheet.status_code, 200, sheet.json)
        self.assertEqual(sheet.json["asset"]["tag"], "E-204")
        with TemporaryDirectory() as temp:
            model = ifcopenshell.open(model_path)
            model.remove(model.by_type("IfcMapConversion")[0])
            bare = Path(temp) / "bare.ifc"
            model.write(bare)
            rejected = upload(bare.read_bytes())
        self.assertEqual(rejected.status_code, 400, rejected.json)
        self.assertIn("map conversion", rejected.json["error"])

    def test_invalid_commands_do_not_fall_back_to_previous_asset(self):
        state = self.run_prompt("P-101, 100 m radius").json["state"]
        unknown = self.run_prompt("P-999, 60 m radius", state)
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("P-999", unknown.json["error"])
        self.assertEqual(self.run_prompt("Set radius to -10 m", state).status_code, 400)


if __name__ == "__main__":
    unittest.main()
