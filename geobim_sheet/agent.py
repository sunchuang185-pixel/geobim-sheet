"""Revision Review Agent: real tool-calling LLM runner (Ollama / OpenAI-compatible) + demo planner."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .core import InputError
from .task_store import TaskStore
from .tools import DEMO_SCENARIOS, ReviewWorkspace, TOOL_SCHEMAS, execute_tool


SYSTEM_PROMPT = """你是一位严谨的 GIS/BIM 设计换版评审工程师 Agent。
所有坐标校验、设备检索、版本比对、管线平面距离计算、前后对照图 PDF 与问题清单 CSV 均必须通过工具计算，严禁自行编造坐标、距离、构件 GlobalId 或文件路径。

工作规范：
1. 第一步先调用 open_project 打开项目并检查坐标系；若返回 incomplete_coordinates，立即停止并向用户逐项说明缺少的具体坐标/CRS 信息，不得猜测。
2. 调用 list_layers 和 compare_revisions 识别图层、位移设备以及 GlobalId 变化候选匹配。
   - 若 compare_revisions 出现 ambiguous_matches（无法唯一确认）或重要设备 GlobalId 变化需要确认，向用户说明匹配依据或候选并请求确认。
3. 若用户提到特定设施（如“泵房”），先调用 find_assets 或 check_utility_proximity；如果返回 needs_disambiguation（存在两个同名泵房），把带位号(Tag)和 GlobalId 的候选列给用户，让用户选择一次后再继续当前任务。
4. 对确认后的设备调用 check_utility_proximity 检查周边给水（water）或其他指定管线；对缺少埋深的管线必须保留并提示“待核实高程”。
5. 最后调用 export_review_package 生成前后对照图 PDF 与问题清单 CSV，并核实返回的文件路径后向用户汇报结果（包含项目 ID、输入版本、构件位号与 GlobalId、管线 ID、缺失高程标记及生成文件）。
"""


@dataclass(frozen=True)
class LLMConfig:
    provider: str  # "ollama" | "openai" | "demo"
    base_url: str
    model: str
    api_key: str
    timeout_s: float = 90.0

    @property
    def is_real_llm(self) -> bool:
        return self.provider in ("ollama", "openai", "openai_compatible") and bool(self.model)

    def public_summary(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model or ("deterministic-demo-planner" if self.provider == "demo" else ""),
            "base_url": self.base_url if self.provider == "ollama" else ("configured" if self.base_url else ""),
            "api_key_configured": bool(self.api_key),
            "is_real_llm": self.is_real_llm,
        }


def load_llm_config() -> LLMConfig:
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    base_url = os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("LLM_MODEL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()

    if not provider:
        if api_key and model:
            provider = "openai"
        elif os.environ.get("OLLAMA_HOST") or (base_url and "11434" in base_url):
            provider = "ollama"
        else:
            provider = "demo"

    if provider == "ollama":
        base_url = base_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        model = model or os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
    elif provider in ("openai", "openai_compatible"):
        provider = "openai"
        base_url = base_url or "https://api.openai.com/v1"
        model = model or "gpt-4o-mini"
    else:
        provider = "demo"

    timeout_s = float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))
    return LLMConfig(provider=provider, base_url=base_url, model=model, api_key=api_key, timeout_s=timeout_s)


def _extract_focus_query(instruction: str) -> str:
    text = instruction.strip()
    tag_match = re.search(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9]*-\d+[A-Za-z0-9_-]*)(?![A-Za-z0-9_-])", text)
    if tag_match:
        return tag_match.group(1).upper()
    for keyword in (
        "渣浆泵房",
        "矿浆泵房",
        "slurry pump",
        "泵房",
        "pump station",
        "pump",
        "浓密机",
        "thickener",
        "选矿主厂房",
        "选矿厂",
        "concentrator",
        "粗碎站",
        "crusher",
        "高位水池",
        "变电所",
        "变电站",
        "substation",
        "水处理",
        "控制室",
        "设备间",
        "仓库",
    ):
        if keyword in text.lower():
            return keyword
    return ""


def _extract_utility_layer(instruction: str) -> str:
    low = instruction.lower()
    if any(k in low for k in ("全部管线", "所有管线", "周边管网", "管网与边坡", "all utilities", "all layers")):
        return "all"
    matched = []
    if any(k in low for k in ("给水", "供水", "回水", "新水", "water")):
        matched.append("water")
    if any(k in low for k in ("矿浆", "尾矿", "渣浆", "slurry", "tailings")):
        matched.append("slurry")
    if any(k in low for k in ("电缆", "供电", "电力", "power", "cable")):
        matched.append("power")
    if any(k in low for k in ("排水", "雨水", "污水", "截洪沟", "drainage")):
        matched.append("drainage")
    if any(k in low for k in ("边坡", "台阶", "slope", "bench")):
        matched.append("slope")
    if len(matched) > 1:
        return "all"
    if len(matched) == 1:
        return matched[0]
    return "water"


def _extract_buffer_m(instruction: str, default: float = 30.0) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:米|m\b|meters?|metres?)", instruction, flags=re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if 5.0 <= val <= 300.0:
            return val
    return default


def _parse_llm_tool_args(raw_args: Any) -> dict:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


class ReviewAgent:
    """Orchestrates the 6 Python review tools via a real tool-calling LLM or deterministic demo planner."""

    def __init__(
        self,
        workspace: ReviewWorkspace,
        store: TaskStore,
        config: LLMConfig | None = None,
    ):
        self.workspace = workspace
        self.store = store
        self.config = config or load_llm_config()

    def _artifact_dir(self, task_id: str) -> Path:
        folder = self.workspace.data_dir / "artifacts" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _run_tool(self, task: dict, name: str, arguments: dict) -> dict:
        started = time.perf_counter()
        args = dict(arguments or {})
        # Ensure open_project uses the task's uploaded files / scenario if omitted by the model
        if name == "open_project":
            if task.get("input_files"):
                args.setdefault("rev_a_path", task["input_files"].get("rev_a_path"))
                args.setdefault("rev_b_path", task["input_files"].get("rev_b_path"))
                args.setdefault("gis_path", task["input_files"].get("gis_path"))
                if task["input_files"].get("crs_override") and not args.get("crs_override"):
                    args["crs_override"] = task["input_files"]["crs_override"]
            elif task.get("scenario") and not args.get("rev_a_path"):
                args.setdefault("preset", task["scenario"])
        elif task.get("project_id") and not args.get("project_id"):
            args["project_id"] = task["project_id"]

        ok = True
        try:
            result = execute_tool(
                name,
                args,
                workspace=self.workspace,
                default_output_dir=str(self._artifact_dir(task["task_id"])),
                confirmed_matches=task.get("confirmed_matches") or None,
            )
        except Exception as error:
            ok = False
            result = {"status": "error", "error": str(error), "missing_info": []}

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        record = {
            "step": len(task["tool_calls"]) + 1,
            "name": name,
            "arguments": args,
            "ok": ok and result.get("status") not in ("error",),
            "status": result.get("status", "ok" if ok else "error"),
            "duration_ms": elapsed_ms,
            "result": result,
        }
        task["tool_calls"].append(record)

        if result.get("project_id") and not task.get("project_id"):
            task["project_id"] = result["project_id"]
        if result.get("missing_info"):
            existing = {
                (x.get("field"), x.get("utility_id") or x.get("source"), x.get("flag") or "")
                for x in task["missing_info"]
            }
            for item in result["missing_info"]:
                sig = (item.get("field"), item.get("utility_id") or item.get("source"), item.get("flag") or "")
                if sig not in existing:
                    existing.add(sig)
                    task["missing_info"].append(item)

        # Only register downloadable artifacts after verifying real files on disk
        if name == "export_review_package" and result.get("status") == "ok":
            verified_files = []
            for f in result.get("files", []):
                fpath = Path(f["path"])
                if fpath.is_file() and fpath.stat().st_size > 0:
                    verified_files.append(
                        {
                            "kind": f["kind"],
                            "label": f["label"],
                            "filename": f["filename"],
                            "path": str(fpath.resolve()),
                            "size_bytes": fpath.stat().st_size,
                            "mimetype": f["mimetype"],
                            "verified": True,
                            "download_url": f"/api/agent/artifacts/{task['task_id']}/{f['filename']}",
                        }
                    )
            task["artifacts"] = verified_files
            task["issues_preview"] = result.get("issues_preview", [])

        self.store.save(task)
        return result

    def start_task(
        self,
        *,
        instruction: str,
        scenario: str | None = None,
        input_files: dict | None = None,
        use_mode: str | None = None,
    ) -> dict:
        if not instruction or not instruction.strip():
            raise InputError("请输入评审任务指令。")
        if scenario and scenario not in DEMO_SCENARIOS:
            raise InputError(f"未知场景：{scenario}")
        if not scenario and not input_files:
            scenario = "normal"

        active_config = self.config
        if use_mode == "demo":
            mode = "demo_planner"
            provider = "demo"
            model = "deterministic-demo-planner"
        elif active_config.is_real_llm:
            mode = "llm"
            provider = active_config.provider
            model = active_config.model
        else:
            mode = "demo_planner"
            provider = "demo"
            model = "deterministic-demo-planner"

        task = self.store.create(
            instruction=instruction.strip(),
            mode=mode,
            provider=provider,
            model=model,
            scenario=scenario,
            input_files=input_files,
        )
        task["messages"].append({"role": "user", "content": instruction.strip()})
        self.store.save(task)

        if mode == "llm":
            return self._run_llm_loop(task)
        return self._run_demo_planner(task)

    def reply_task(
        self,
        task_id: str,
        *,
        reply: str = "",
        selected_tag: str | None = None,
        selected_guid: str | None = None,
        confirm_matches: list[dict] | None = None,
    ) -> dict:
        task = self.store.get(task_id)
        if task is None:
            raise InputError(f"任务 {task_id} 不存在。")

        # Re-open project in workspace if process restarted
        if task.get("project_id") and self.workspace.get_project(task["project_id"]) is None:
            open_args = {"project_id": task["project_id"]}
            if task.get("input_files"):
                open_args.update(
                    {
                        "rev_a_path": task["input_files"].get("rev_a_path"),
                        "rev_b_path": task["input_files"].get("rev_b_path"),
                        "gis_path": task["input_files"].get("gis_path"),
                    }
                )
            elif task.get("scenario"):
                open_args["preset"] = task["scenario"]
            execute_tool("open_project", open_args, workspace=self.workspace)

        if selected_tag:
            task["selected_asset_tag"] = selected_tag.strip()
        if selected_guid:
            task["selected_asset_guid"] = selected_guid.strip()
        if confirm_matches:
            task["confirmed_matches"].extend(confirm_matches)

        # Also extract tag from free-text reply if user typed e.g. "选 P-102"
        if reply and not task.get("selected_asset_tag"):
            m = re.search(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9]*-\d+[A-Za-z0-9_-]*)(?![A-Za-z0-9_-])", reply)
            if m:
                task["selected_asset_tag"] = m.group(1).upper()

        # If pending question was ID-change match confirmation and user affirmed
        pending = task.get("pending_question") or {}
        if pending.get("kind") == "confirm_id_match" and not confirm_matches:
            low = (reply or "").lower()
            if any(w in low for w in ("确认", "是", "同意", "继续", "ok", "yes", "confirm")):
                for cand in pending.get("candidates", []):
                    if cand.get("rev_a_guid") and cand.get("rev_b_guid"):
                        task["confirmed_matches"].append(
                            {"rev_a_guid": cand["rev_a_guid"], "rev_b_guid": cand["rev_b_guid"]}
                        )

        user_msg = reply.strip()
        if not user_msg:
            if selected_tag:
                user_msg = f"我选择设备位号 {selected_tag}，请继续原评审任务并生成对照图和问题清单。"
            elif confirm_matches:
                user_msg = "我确认上述 GlobalId 变化匹配关系，请继续原评审任务并生成对照图和问题清单。"
            else:
                user_msg = "请继续当前评审任务。"

        task["messages"].append({"role": "user", "content": user_msg})
        task["pending_question"] = None
        task["status"] = "running"
        self.store.save(task)

        if task["mode"] == "llm":
            return self._run_llm_loop(task)
        return self._run_demo_planner(task)

    # ---------- Real Tool-Calling LLM Loop (Ollama & OpenAI-compatible) ----------

    def _call_llm(self, llm_messages: list[dict]) -> dict:
        cfg = self.config
        if cfg.provider == "ollama" and not cfg.base_url.endswith("/v1"):
            url = f"{cfg.base_url}/api/chat"
            payload = {
                "model": cfg.model,
                "messages": llm_messages,
                "tools": TOOL_SCHEMAS,
                "stream": False,
            }
            headers = {"Content-Type": "application/json"}
            with httpx.Client(timeout=cfg.timeout_s) as client:
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            msg = data.get("message") or {}
            tool_calls = []
            for idx, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                tool_calls.append(
                    {
                        "id": tc.get("id") or f"call_{idx + 1}",
                        "name": fn.get("name"),
                        "arguments": _parse_llm_tool_args(fn.get("arguments")),
                    }
                )
            return {"content": msg.get("content") or "", "tool_calls": tool_calls}

        # OpenAI-compatible (/v1/chat/completions — supports OpenAI, Gemini, DeepSeek, Ollama /v1, vLLM)
        url = f"{cfg.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        payload = {
            "model": cfg.model,
            "messages": llm_messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": 0.1,
        }
        with httpx.Client(timeout=cfg.timeout_s) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = []
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tool_calls.append(
                {
                    "id": tc.get("id") or f"call_{idx + 1}",
                    "name": fn.get("name"),
                    "arguments": _parse_llm_tool_args(fn.get("arguments")),
                }
            )
        return {"content": msg.get("content") or "", "tool_calls": tool_calls}

    def _build_llm_messages(self, task: dict) -> list[dict]:
        context_note = ""
        if task.get("input_files"):
            f = task["input_files"]
            context_note = (
                f"\n当前任务输入文件：rev_a_path={f.get('rev_a_path')}, "
                f"rev_b_path={f.get('rev_b_path')}, gis_path={f.get('gis_path')}"
            )
        elif task.get("scenario"):
            context_note = f"\n当前任务使用内置合成场景 preset='{task['scenario']}'，可直接调用 open_project(preset='{task['scenario']}')。"
        if task.get("project_id"):
            context_note += f"\n当前已打开 project_id='{task['project_id']}'。"
        if task.get("selected_asset_tag"):
            context_note += f"\n用户已确认目标设备位号：{task['selected_asset_tag']}。"

        messages = [{"role": "system", "content": SYSTEM_PROMPT + context_note}]
        for m in task["messages"]:
            messages.append(m)
        return messages

    def _run_llm_loop(self, task: dict, max_steps: int = 12) -> dict:
        last_disambiguation: dict | None = None
        last_id_confirmation: dict | None = None

        try:
            for _ in range(max_steps):
                llm_messages = self._build_llm_messages(task)
                reply = self._call_llm(llm_messages)
                tool_calls = reply["tool_calls"]
                content = reply["content"].strip()

                if not tool_calls:
                    task["messages"].append({"role": "assistant", "content": content})
                    if last_disambiguation and not task.get("artifacts"):
                        task["status"] = "waiting_for_user"
                        task["pending_question"] = {
                            "kind": "disambiguate_asset",
                            "question": content or last_disambiguation.get("question", "请选择具体设备位号："),
                            "candidates": last_disambiguation.get("candidates", []),
                        }
                    elif last_id_confirmation and not task.get("artifacts"):
                        task["status"] = "waiting_for_user"
                        task["pending_question"] = {
                            "kind": "confirm_id_match",
                            "question": content or "请确认上述 GlobalId 变化设备的对应关系：",
                            "candidates": last_id_confirmation.get("candidates", []),
                        }
                    else:
                        task["status"] = "completed"
                        task["final_summary"] = content
                    return self.store.save(task)

                # Record assistant tool_calls turn in OpenAI message schema
                task["messages"].append(
                    {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )

                for tc in tool_calls:
                    result = self._run_tool(task, tc["name"], tc["arguments"])
                    if result.get("status") == "needs_disambiguation":
                        last_disambiguation = result
                    elif result.get("status") == "needs_confirmation" or (
                        tc["name"] == "compare_revisions"
                        and result.get("id_changed_candidates")
                        and not task.get("confirmed_matches")
                    ):
                        last_id_confirmation = {
                            "candidates": result.get("id_changed_candidates", []) or result.get("ambiguous_matches", [])
                        }
                    elif tc["name"] == "export_review_package" and result.get("status") == "ok":
                        last_disambiguation = None
                        last_id_confirmation = None

                    task["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["name"],
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                self.store.save(task)

            task["status"] = "completed"
            task["final_summary"] = "已达到最大工具调用步数上限，请查看工具执行轨迹和生成文件。"
            return self.store.save(task)
        except Exception as error:
            task["status"] = "failed"
            task["error"] = f"调用模型失败（{self.config.provider}/{self.config.model}）：{error}"
            task["messages"].append({"role": "assistant", "content": task["error"]})
            return self.store.save(task)

    # ---------- Deterministic Demo Tool Planner (same 6 Python tools, zero hardcoded metrics) ----------

    def _run_demo_planner(self, task: dict) -> dict:
        instruction = task["instruction"]
        utility_layer = _extract_utility_layer(instruction)
        buffer_m = _extract_buffer_m(instruction, default=30.0)
        focus_query = task.get("selected_asset_tag") or _extract_focus_query(instruction)

        # Step 1: open_project if not yet opened
        if not task.get("project_id"):
            open_res = self._run_tool(task, "open_project", {})
            if open_res.get("status") != "ok":
                reasons = "\n".join(f"- **{m['field']}** ({m['source']}): {m['message']}（需要：{m['required']}）" for m in open_res.get("missing_info", []))
                summary = (
                    f"无法确认项目坐标，已停止换版比对与出图。\n\n缺失信息：\n{reasons or open_res.get('error', '未知错误')}"
                )
                task["status"] = "completed"
                task["final_summary"] = summary
                task["messages"].append({"role": "assistant", "content": summary})
                return self.store.save(task)

        project_id = task["project_id"]

        # Step 2: list_layers (if not called yet)
        if not any(tc["name"] == "list_layers" for tc in task["tool_calls"]):
            self._run_tool(task, "list_layers", {"project_id": project_id})

        # Step 3: compare_revisions
        cmp_res = self._run_tool(
            task,
            "compare_revisions",
            {"project_id": project_id, "confirmed_matches": task.get("confirmed_matches") or None},
        )
        if cmp_res.get("status") == "incomplete_coordinates":
            task["status"] = "completed"
            task["final_summary"] = "坐标系不完整，无法继续比对。"
            task["messages"].append({"role": "assistant", "content": task["final_summary"]})
            return self.store.save(task)

        # If there are ambiguous ID matches that cannot be uniquely confirmed, ask user first
        if cmp_res.get("ambiguous_matches") and not task.get("confirmed_matches"):
            amb = cmp_res["ambiguous_matches"]
            question = "存在 GlobalId 变化且无法唯一匹配的构件，请确认映射关系后继续：\n" + "\n".join(
                f"- {item['basis']}" for item in amb
            )
            task["status"] = "waiting_for_user"
            task["pending_question"] = {
                "kind": "confirm_id_match",
                "question": question,
                "candidates": amb,
            }
            task["messages"].append({"role": "assistant", "content": question})
            return self.store.save(task)

        # Step 4: find_assets to resolve target facility (e.g. "泵房")
        focus_tag = task.get("selected_asset_tag")
        focus_guid = task.get("selected_asset_guid")
        if not focus_tag and not focus_guid and focus_query:
            find_res = self._run_tool(task, "find_assets", {"project_id": project_id, "query": focus_query})
            if find_res.get("status") == "needs_disambiguation":
                question = find_res["question"]
                task["status"] = "waiting_for_user"
                task["pending_question"] = {
                    "kind": "disambiguate_asset",
                    "question": question,
                    "candidates": find_res.get("candidates", []),
                }
                task["messages"].append({"role": "assistant", "content": question})
                return self.store.save(task)
            if find_res.get("assets"):
                focus_tag = find_res["assets"][0].get("tag")
                focus_guid = find_res["assets"][0].get("guid")

        # Step 5: check_utility_proximity
        prox_args = {
            "project_id": project_id,
            "layer": utility_layer,
            "buffer_m": buffer_m,
        }
        if focus_tag:
            prox_args["asset_tag"] = focus_tag
        elif focus_guid:
            prox_args["asset_guid"] = focus_guid
        elif focus_query:
            prox_args["asset_query"] = focus_query

        prox_res = self._run_tool(task, "check_utility_proximity", prox_args)
        if prox_res.get("status") == "needs_disambiguation":
            question = prox_res["question"]
            task["status"] = "waiting_for_user"
            task["pending_question"] = {
                "kind": "disambiguate_asset",
                "question": question,
                "candidates": prox_res.get("candidates", []),
            }
            task["messages"].append({"role": "assistant", "content": question})
            return self.store.save(task)

        target_asset = prox_res.get("asset") or {}
        if target_asset.get("tag"):
            focus_tag = target_asset["tag"]
        if target_asset.get("rev_b_guid"):
            focus_guid = target_asset["rev_b_guid"]

        # Step 6: export_review_package
        export_args = {
            "project_id": project_id,
            "utility_layer": utility_layer,
            "buffer_m": buffer_m,
            "radius_m": 80.0,
            "include_dxf": True,
        }
        if focus_tag:
            export_args["focus_asset_tag"] = focus_tag
        elif focus_guid:
            export_args["focus_asset_guid"] = focus_guid

        export_res = self._run_tool(task, "export_review_package", export_args)

        # Compose concise, fully traceable summary purely from tool results
        moved_lines = [
            f"- **{m['tag']} ({m['name']})**：位移 `{m['displacement_m']:.2f} m`（ΔE={m['dx_m']:+.2f}m, ΔN={m['dy_m']:+.2f}m）；"
            f"匹配方式：`{m['match_method']}`（Rev A GUID: `{m['rev_a_guid']}` → Rev B GUID: `{m['rev_b_guid']}`）"
            + (f" — {m['match_basis']}" if m.get("id_changed") else "")
            for m in cmp_res.get("moved_assets", [])
        ]
        prox_lines = [
            f"- **{p['utility_id']} ({p['utility_name']})**：Rev A 距 `{p['distance_rev_a_m']:.2f} m` → Rev B 距 `{p['distance_rev_b_m']:.2f} m`"
            f"（`{p['elevation_note']}`）— {p['review_reason']}"
            for p in prox_res.get("items", [])
        ]
        file_lines = [
            f"- {f['label']}：`{f['filename']}`（{f['size_bytes']} bytes，已核验落盘）"
            for f in task.get("artifacts", [])
        ]
        layer_titles = {
            "all": "全部管线与边坡",
            "water": "给水管线",
            "slurry": "矿浆/尾矿管线",
            "power": "电力电缆沟",
            "drainage": "排水/截洪沟",
            "slope": "台阶边坡线",
        }
        layer_title = layer_titles.get(utility_layer, utility_layer)
        trace = export_res.get("traceability", {})
        summary = (
            f"### 设计换版评审已完成（项目 `{project_id}` / `{trace.get('crs', '')}`）\n\n"
            f"**输入溯源**：Rev A `{trace.get('rev_a_file', '')}` · Rev B `{trace.get('rev_b_file', '')}` · GIS `{trace.get('gis_file', '')}`\n\n"
            f"**1. 位置变动设备（{cmp_res.get('moved_count', 0)} 项）**\n"
            + ("\n".join(moved_lines) if moved_lines else "- 未检测到超过阈值的平面位移。")
            + f"\n\n**2. {target_asset.get('tag', focus_tag or '目标设备')} 周边 {buffer_m:g} m 需复核{layer_title}（{prox_res.get('review_count', 0)} 项，缺埋深 {prox_res.get('missing_elevation_count', 0)} 项）**\n"
            + ("\n".join(prox_lines) if prox_lines else "- 复核范围内未发现管线。")
            + "\n\n**3. 已生成评审交付件**\n"
            + ("\n".join(file_lines) if file_lines else "- 未生成文件。")
        )
        task["status"] = "completed"
        task["final_summary"] = summary
        task["messages"].append({"role": "assistant", "content": summary})
        return self.store.save(task)
