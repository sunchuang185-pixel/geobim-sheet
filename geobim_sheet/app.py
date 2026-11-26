import os
import re
from collections import OrderedDict
from dataclasses import asdict
from functools import lru_cache
from io import BytesIO
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from threading import Lock
from uuid import uuid4

from flask import Flask, jsonify, request, send_file
from shapely.errors import ShapelyError

from .agent import LLMConfig, ReviewAgent, load_llm_config
from .core import InputError, compose, geometry_json, load_project, select
from .exports import dxf_bytes, pdf_bytes
from .task_store import TaskStore
from .tools import DEMO_SCENARIOS, ReviewWorkspace, default_data_dir


DEMO_DIR = Path(__file__).with_name("demo")


@lru_cache(maxsize=1)
def demo_project():
    return load_project(
        DEMO_DIR / "demoworks-r03.ifc",
        DEMO_DIR / "demoworks-site-r02.geojson",
        "demoworks-r03.ifc",
        "demoworks-site-r02.geojson",
    )


def create_app(*, data_dir: str | Path | None = None, llm_config: LLMConfig | None = None):
    app = Flask(__name__, static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
    projects = OrderedDict()
    lock = Lock()

    storage_dir = Path(data_dir) if data_dir else default_data_dir()
    workspace = ReviewWorkspace(storage_dir)
    task_store = TaskStore(storage_dir)
    agent = ReviewAgent(workspace, task_store, config=llm_config or load_llm_config())
    allow_upload = os.environ.get("GEOBIM_ALLOW_UPLOAD", "true").strip().lower() not in ("0", "false", "no")

    app.extensions["geobim_workspace"] = workspace
    app.extensions["geobim_task_store"] = task_store
    app.extensions["geobim_agent"] = agent

    @app.errorhandler(InputError)
    def bad_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(ShapelyError)
    def bad_geometry(error):
        app.logger.info("Geometry processing stopped: %s", error)
        return jsonify(error="A geometry could not be processed. Check the IFC and GIS exports for malformed geometry."), 400

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Use an IFC and GeoJSON export under 64 MiB in total."), 413

    # ---------- Existing Sheet Workspace Routes ----------

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/demo")
    def demo():
        return jsonify(project="demo", **demo_project().summary())

    @app.post("/api/projects")
    def import_project():
        model = request.files.get("ifc")
        site = request.files.get("gis")
        if not model or not site or not model.filename.lower().endswith(".ifc") or not site.filename.lower().endswith((".geojson", ".json")):
            raise InputError("Choose an .ifc model and a .geojson site file.")
        with TemporaryDirectory(prefix="geobim-sheet-") as folder:
            model_path = Path(folder) / "model.ifc"
            site_path = Path(folder) / "site.geojson"
            model.save(model_path)
            site.save(site_path)
            project = load_project(model_path, site_path, PureWindowsPath(model.filename).name, PureWindowsPath(site.filename).name)
        key = uuid4().hex
        with lock:
            projects[key] = project
            if len(projects) > 4:
                projects.popitem(last=False)
        return jsonify(project=key, **project.summary()), 201

    def project_and_selection():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("project"), str):
            raise InputError("Load a project before creating a drawing.")
        key = data["project"]
        if key == "demo":
            project = demo_project()
        else:
            with lock:
                project = projects.get(key)
                if project:
                    projects.move_to_end(key)
        if project is None:
            raise InputError("This project has expired. Import the two files again.")
        selection, asset = select(project, data.get("state"), data.get("prompt", ""), data.get("layers"))
        return project, selection, asset, compose(project, selection, asset)

    @app.post("/api/sheet")
    def sheet():
        _, selection, asset, (center, features, subject) = project_and_selection()
        return jsonify(
            state=asdict(selection),
            asset={"tag": asset.tag, "name": asset.name},
            center=[center.x, center.y],
            features={key: [geometry_json(geometry) for geometry in geometries] for key, geometries in features.items()},
            subject=geometry_json(subject),
        )

    @app.post("/api/export/<kind>")
    def export(kind):
        if kind not in {"pdf", "dxf"}:
            raise InputError("Export format must be PDF or DXF.")
        project, selection, asset, drawing = project_and_selection()
        if kind == "pdf":
            content, mimetype = pdf_bytes(project, selection, asset, drawing), "application/pdf"
        else:
            content, mimetype = dxf_bytes(selection, asset, drawing), "application/dxf"
        label = re.sub(r"[^a-zA-Z0-9_-]+", "-", asset.tag or asset.name).strip("-") or "asset"
        filename = f"site-sheet-{label}-{selection.radius:g}m.{kind}"
        return send_file(BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=filename)

    # ---------- Dedicated Revision Review Agent Routes ----------

    @app.get("/agent")
    def agent_page():
        return app.send_static_file("agent.html")

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", llm=agent.config.public_summary())

    @app.get("/api/agent/config")
    def agent_config():
        return jsonify(
            llm=agent.config.public_summary(),
            allow_upload=allow_upload,
            scenarios=[
                {"id": key, "label": info["label"], "label_en": info["label_en"]}
                for key, info in DEMO_SCENARIOS.items()
            ],
            default_prompt="对比这两版设计，找出位置变动的设备。检查泵房附近有哪些给水管线需要复核，生成一张前后对照图和一份问题清单。",
            recent_tasks=task_store.list_recent(10),
        )

    @app.post("/api/agent/tasks")
    def create_agent_task():
        input_files = None
        scenario = None
        use_mode = None

        if request.content_type and "multipart/form-data" in request.content_type:
            instruction = (request.form.get("instruction") or "").strip()
            scenario = (request.form.get("scenario") or "").strip() or None
            use_mode = (request.form.get("mode") or "").strip() or None
            rev_a = request.files.get("rev_a")
            rev_b = request.files.get("rev_b")
            gis = request.files.get("gis")
            if rev_a or rev_b or gis:
                if not allow_upload:
                    raise InputError("当前为公开演示模式，已禁用自定义文件上传；请自托管部署后开启 GEOBIM_ALLOW_UPLOAD=true。")
                if not (rev_a and rev_b and gis):
                    raise InputError("自定义评审需同时上传 Rev A (.ifc)、Rev B (.ifc) 和 GIS (.geojson) 三个文件。")
                if not rev_a.filename.lower().endswith(".ifc") or not rev_b.filename.lower().endswith(".ifc"):
                    raise InputError("两版模型文件必须为 .ifc 格式。")
                if not gis.filename.lower().endswith((".geojson", ".json", ".dxf")):
                    raise InputError("管线图层必须为 .geojson、.json 或 .dxf 格式。")
                upload_dir = storage_dir / "uploads" / uuid4().hex[:10]
                upload_dir.mkdir(parents=True, exist_ok=True)
                ra_name = PureWindowsPath(rev_a.filename).name
                rb_name = PureWindowsPath(rev_b.filename).name
                gis_name = PureWindowsPath(gis.filename).name
                ra_path = upload_dir / f"rev-a-{ra_name}"
                rb_path = upload_dir / f"rev-b-{rb_name}"
                gis_path = upload_dir / gis_name
                rev_a.save(ra_path)
                rev_b.save(rb_path)
                gis.save(gis_path)
                crs_override = None
                override_crs = (request.form.get("override_crs") or "").strip()
                if override_crs:
                    try:
                        crs_override = {
                            "crs": override_crs,
                            "eastings": float(request.form.get("override_eastings") or 0.0),
                            "northings": float(request.form.get("override_northings") or 0.0),
                            "rotation_deg": float(request.form.get("override_rotation_deg") or 0.0),
                        }
                    except ValueError as err:
                        raise InputError("显式配准原点坐标与旋转角必须为数字。") from err
                input_files = {
                    "rev_a_path": str(ra_path.resolve()),
                    "rev_b_path": str(rb_path.resolve()),
                    "gis_path": str(gis_path.resolve()),
                    "rev_a_name": ra_name,
                    "rev_b_name": rb_name,
                    "gis_name": gis_name,
                    "crs_override": crs_override,
                }
                scenario = None
        else:
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict):
                raise InputError("Invalid JSON body.")
            instruction = (data.get("instruction") or "").strip()
            scenario = (data.get("scenario") or "").strip() or None
            use_mode = (data.get("mode") or "").strip() or None
            if data.get("input_files") and isinstance(data["input_files"], dict):
                input_files = data["input_files"]

        task = agent.start_task(
            instruction=instruction,
            scenario=scenario,
            input_files=input_files,
            use_mode=use_mode,
        )
        return jsonify(task), 201

    @app.get("/api/agent/tasks/<task_id>")
    def get_agent_task(task_id):
        task = task_store.get(task_id)
        if task is None:
            return jsonify(error=f"Task {task_id} not found."), 404
        return jsonify(task)

    @app.post("/api/agent/tasks/<task_id>/reply")
    def reply_agent_task(task_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise InputError("Invalid JSON body.")
        task = agent.reply_task(
            task_id,
            reply=str(data.get("reply") or ""),
            selected_tag=data.get("selected_tag"),
            selected_guid=data.get("selected_guid"),
            confirm_matches=data.get("confirm_matches"),
        )
        return jsonify(task)

    @app.get("/api/agent/artifacts/<task_id>/<path:filename>")
    def download_agent_artifact(task_id, filename):
        task = task_store.get(task_id)
        if task is None:
            return jsonify(error="Task not found."), 404
        clean_name = Path(filename).name
        artifact = next((a for a in task.get("artifacts", []) if a["filename"] == clean_name and a.get("verified")), None)
        if artifact is None:
            return jsonify(error="Requested artifact has not been generated yet."), 404
        file_path = Path(artifact["path"]).resolve()
        allowed_root = (storage_dir / "artifacts").resolve()
        if not str(file_path).startswith(str(allowed_root)) or not file_path.is_file():
            return jsonify(error="Artifact file is missing on disk."), 404
        return send_file(
            BytesIO(file_path.read_bytes()),
            mimetype=artifact.get("mimetype") or "application/octet-stream",
            as_attachment=True,
            download_name=clean_name,
        )

    return app
