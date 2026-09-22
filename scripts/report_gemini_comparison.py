"""Audit Gemini's wire prompts and render an updating four-backend comparison."""
import json
from pathlib import Path

from scripts.run_unified_retest import save
from scripts.summarize_unified_comparison import audit_call, audit_episode
from scripts.unified_chat_brain import chat_body

ROOT = Path(__file__).resolve().parents[1]
TASKS = [("hard-dna", "DNA"), ("hard-music", "Music"), ("hard-wwii", "World War II")]


def latest_progress(path):
    # A live writer may be midway through its final JSONL record.
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 1024 * 1024))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def check_call(call):
    audit_call(call)
    assert call["chat_request"] == chat_body(call["request"]), "chat wire prompt differs from canonical input"


def verify_probe(probe, baseline):
    assert probe["complete"]
    for old in baseline:
        assert old["fixture_sha256"] == probe["fixture_sha256"]
        assert old["instruction"] == probe["instruction"]
        assert [m["canonical_sha256"] for m in old["measurements"]] == [m["canonical_sha256"] for m in probe["measurements"]]
        assert [c["call"]["canonical"]["sha256"] for c in old["controls"]] == [c["call"]["canonical"]["sha256"] for c in probe["controls"]]
        for previous, current in zip(old["full_first_pages"], probe["full_first_pages"]):
            assert [g["canonical_sha256"] for g in previous["debug"]["unified_rounds"][0]] == [g["canonical_sha256"] for g in current["debug"]["unified_rounds"][0]]
    for row in probe["measurements"]:
        for call in row["calls"]:
            check_call(call)
    for row in probe["controls"]:
        check_call(row["call"])
    for row in probe["full_first_pages"]:
        for call in row["debug"]["unified_calls"]:
            check_call(call)


def render(out_root, baseline_root, manifest):
    result = {"manifest": manifest, "episodes": [], "probes": [], "progress": []}
    pause_path = baseline_root / "pause.json"
    pause = json.loads(pause_path.read_text()) if pause_path.exists() else {}
    if pause:
        result["baseline_pause"] = pause
    labels = {"jev": "Jev", "semif": "SemIf", "laya-mlx": "Laya MLX", "gemini": "Gemini 3.8 Flash"}
    roots = {name: baseline_root for name in ("jev", "semif", "laya-mlx")}
    roots["gemini"] = out_root
    for name, root in roots.items():
        path = root / "probe" / name / "probe.json"
        if path.exists():
            probe = json.loads(path.read_text())
            result["probes"].append({"brain": name, "complete": probe.get("complete", False),
                "measurements": [{k: v for k, v in m.items() if k != "calls"} for m in probe["measurements"]],
                "controls_correct": sum(c["correct"] for c in probe["controls"]), "controls_total": len(probe["controls"]),
                "source": str(path)})
        for task, _ in TASKS:
            path = root / "race" / name / (task + ".summary.json")
            if path.exists():
                row = json.loads(path.read_text())
                if name == "gemini":
                    full = path.with_name(task + ".json")
                    if full.exists():
                        raw = json.loads(full.read_text())
                        row["audit"] = audit_episode(raw)
                        for step in raw["trace"]:
                            for call in step["brain_debug"].get("unified_calls", []):
                                check_call(call)
                result["episodes"].append({"source": str(path), **row})
            else:
                progress = root / "race" / name / (task + ".progress.jsonl")
                if progress.exists() and progress.stat().st_size:
                    last = latest_progress(progress)
                    if last:
                        result["progress"].append({"brain": name, **last})
    rows = {(r["brain"], r["task_id"]): r for r in result["episodes"]}
    phase = manifest.get("phase", "preparing")
    phases = {"probe": "固定输入测试中", "waiting_for_baseline": "固定输入已完成，等待 Laya 完成后开始三题",
              "race": "完整三题测试中", "complete": "Gemini 三题全部结束", "stopped": "测试已停止，详见错误记录"}
    lines = ["# Gemini 与 Jev / SemIf / Laya 的统一条件对照", "",
             f"状态：{phases.get(phase, phase)}。", "",
             "请求模型：`models/gemini-3.8-flash`；服务地址：`https://api.hotday.uk/v1`。模型名称按接口请求与返回值记录。",
             "沿用 unified-choice-v1：相同指令、目标/当前页简介、最近六步、完整标题和短上下文；",
             "所有候选参与分组，每组最多 255 项，组胜者再次比较。没有新增路线提示、人工选路或搜索工具。",
             "Gemini 额外的系统文字仅约束输出格式；严格 JSON schema 限定 choice 为本组合法编号。",
             '输出形式：`{"answers":{"next":{"choice":"A"}}}`。温度 0，输出上限 2,048 tokens；非法或截断回答显式报错。',
             "可检查 [DNA 的实际 255 项请求样例](2026-09-21-gemini-prompt-example.json)。",
             "不发送旧模型评分、完整 URL 或其他额外证据；记录实际 HTTP 请求体、返回、token 用量和输入哈希。",
             ("本地 GPU 测试进行期间仅发送固定快照的云端请求；完整浏览器比赛等待原三模型实验结束后再开始。"
              if manifest.get("live_race_waits_for_baseline") else
              "Gemini 使用云端 API，不占用本地 GPU；完整比赛无需等待 Laya，本轮与本地测试并行。浏览器共享本机 CPU 与网络，耗时按实际并行条件记录。"), "",
             "## 固定 255 项选择", "", "每页预热后测三次取中位数，包含网络调用；不含浏览器和模型加载。", "",
             "| 页面 | Jev | SemIf | Laya MLX | Gemini 3.8 Flash |", "|---|---:|---:|---:|---:|"]
    probes = {p["brain"]: p for p in result["probes"]}
    for index, (_, title) in enumerate(TASKS):
        cells = []
        for name in labels:
            measures = probes.get(name, {}).get("measurements", [])
            cells.append(f"{measures[index]['median_seconds']:.3f} 秒" if len(measures) > index else "待完成")
        lines.append("| " + title + " | " + " | ".join(cells) + " |")
    lines += ["", "基础目标识别诊断（目标放在第 1、128、255 位，共九次）：", ""]
    for name in labels:
        if name in probes:
            p = probes[name]
            lines.append(f"- {labels[name]}：{p['controls_correct']}/{p['controls_total']}；" + ("已完成。" if p["complete"] else "进行中。"))
    lines += ["", "## 完整三题", "", "表内耗时不含初始化，每题最多 60 分钟；此前程序中断尝试另存于原报告，不合并进最后一次运行。", "",
              "| 题目 | Jev | SemIf | Laya MLX | Gemini 3.8 Flash |", "|---|---|---|---|---|"]
    for task, title in TASKS:
        cells = []
        for name in labels:
            row = rows.get((name, task))
            if row:
                status = "成功" if row["status"] == "success" else "超时" if row["reason"] == "timeout" else "错误"
                cells.append(f"{status}；{row['clicks']} 次；{row['seconds']:.1f} 秒")
            else:
                progress = next((p for p in result["progress"] if p["brain"] == name and p["task_id"] == task), None)
                if pause.get("status") == "paused" and pause.get("brain") == name and pause.get("task_id") == task:
                    cells.append("用户暂停；未完成")
                else:
                    cells.append(f"运行中；{progress['clicks_so_far']} 次" if progress else "待运行")
        lines.append("| " + title + " | " + " | ".join(cells) + " |")
    if pause.get("status") == "paused":
        lines += ["", f"Laya 二战题于 {pause.get('elapsed_seconds', 0):.1f} 秒时按用户要求停止；不是超时结果。",
                  "暂停前已确认 " + str(pause.get("confirmed_clicks")) + " 次点击；之后的一次导航被中断。",
                  "保留逐步进度，等待用户明天通知继续，不自动重启。进程结束后无法恢复内存状态，续测时只重跑未完成的二战题，旧尝试另存。"]
    gemini_rows = [rows.get(("gemini", task)) for task, _ in TASKS]
    jev_rows = [rows.get(("jev", task)) for task, _ in TASKS]
    if all(gemini_rows) and all(jev_rows):
        gemini_clicks = sum(r["clicks"] for r in gemini_rows)
        jev_clicks = sum(r["clicks"] for r in jev_rows)
        gemini_seconds = sum(r["seconds"] for r in gemini_rows)
        jev_seconds = sum(r["seconds"] for r in jev_rows)
        lines += ["", f"Gemini 成功 {sum(r['status']=='success' for r in gemini_rows)}/3，总点击 {gemini_clicks} 次，总动作耗时 {gemini_seconds:.3f} 秒；",
                  f"Jev 成功 {sum(r['status']=='success' for r in jev_rows)}/3，总点击 {jev_clicks} 次，总动作耗时 {jev_seconds:.3f} 秒。",
                  f"本轮 Gemini 路线点击更少，动作耗时约为 Jev 的 {gemini_seconds / jev_seconds:.2f} 倍。"]
    if manifest.get("fixed_input_parity_verified"):
        lines += ["", "固定测试的 255 项、九次目标诊断及首轮完整页面分组，已与三个已有后端逐一核验输入哈希一致。"]
    if manifest.get("reused_probe_from"):
        lines += ["", "固定测试复用并重新审计：`" + manifest["reused_probe_from"] + "`。",
                  "首次浏览器运行被旧整页模式白名单拦截，0 次点击；旧日志保留在 `runs/gemini-unified/2026-09-21/`。",
                  "已修复整页能力声明与跨题 HTTP 连接生命周期；prompt、候选分组和返回格式不变。"]
    if manifest.get("error"):
        lines += ["", "停止原因：`" + manifest["error"] + "`。"]
    lines += ["", "三道题各一次运行只能作为本轮系统对照。真实网页会变化，网络延迟与模型推理均计入动作时间。",
              "[原三模型实验](2026-09-21-unified-comparison.md) · [机器可读结果](2026-09-21-gemini-unified-summary.json)", ""]
    save(ROOT / "reports/2026-09-21-gemini-unified-summary.json", result)
    path = ROOT / "reports/2026-09-21-gemini-unified-comparison.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)
    return result
