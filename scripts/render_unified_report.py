"""Render the audited JSON summary, explicitly distinguishing pending results."""
import argparse
import json
from pathlib import Path


def render(summary):
    names = ["jev", "laya-mlx", "semif"]
    labels = {"jev":"Jev", "laya-mlx":"Laya MLX", "semif":"SemIf"}
    tasks = [("hard-dna","DNA → Manipuri pony"), ("hard-music","Music → 2001 AAA Championships"),
             ("hard-wwii","World War II → Bald Mountain Recreation Area")]
    probes = {p["brain"]:p for p in summary["probes"]}
    episodes = {(e["brain"],e["task_id"]):e for e in summary["episodes"]}
    status_line = ("完整三题比赛已全部结束。" if summary["complete"] else
                   f"完整三题比赛已完成 {len(episodes)}/9 次运行，剩余 {len(summary['pending'])} 次。")
    pause = summary.get("pause", {})
    if pause.get("status") == "paused":
        status_line += " **Laya 已按用户要求暂停，等待用户通知后再继续，不会自动重启。**"
    elif summary.get("execution_status") == "stopped":
        status_line += " **测试进程已停止，剩余任务没有在运行。**"
    elif summary.get("execution_status") == "running":
        status_line += " 后台测试进程正在运行。"
    lines = ["# 统一提示词与输入条件后的对照实验", "",
        "2026-09-21，Apple M1 Max / 32 GiB。固定输入实验已完成，三者共享输入哈希核验通过。",
        status_line, "",
        "## 统一条件", "",
        "规则与字段详见 [统一协议](2026-09-21-unified-protocol.md)，实际输入见 [DNA 255 项样例](2026-09-21-unified-prompt-example.json)。",
        "三者使用相同目标/当前页简介、最近六步、候选完整标题与最多 48 字符的短上下文。",
        "所有合规链接按同一套预算规则分组比较，无某个模型独有的 Score 预筛选。", "",
        "固定实验来自同一份保存页面：DNA 456 项、Music 731 项、World War II 1,141 项。",
        "固定 255 项取每页原顺序的前 255 项，没有经过 Jev 筛选；预热后各测三次，取中位数。",
        "计时包含后端请求、调用前校验与读出，不含模型加载、公共分组规划和网页操作；Jev 网络耗时计入。",
        "两个本地模型依次使用 Apple GPU。", "", "## 同一批 255 项的单次选择", "",
        "| 页面 | Jev | Laya MLX | SemIf |", "|---|---:|---:|---:|"]
    for i,(task,title) in enumerate(tasks):
        lines.append("| "+title.split(" → ")[0]+" | "+" | ".join(
            f"{probes[n]['measurements'][i]['median_seconds']:.3f} 秒" for n in names)+" |")
    lines += ["", "候选信息比之前的标题精简实验更长，输入来源也不同，不能把前后变化全部归因于提示词。",
              "Laya 实际输入为 5,352–5,666 tokens，SemIf 为 7,459–7,905；两者实际编码均与公共规划器一致。",
              "", "## 简单目标识别诊断", "",
              "把目标标题放在第 1、128、255 位，共九次；沿用普通候选 ID，避免编号泄露答案。", "",
              "| 后端 | 选对目标 |", "|---|---:|"]
    for name in names:
        lines.append(f"| {labels[name]} | {probes[name]['controls_correct']}/9 |")
    lines += ["", "SemIf 漏选了二战快照中位于最后一项的目标；Laya 九次均未选对。",
              "这些是基础诊断，不代表真实 WikiRace 成功率。统一规则没有自动解决本地模型的选择错误。",
              "", "## 完整首步页面的离线选择", "",
              "这里全部合规候选参加比较，三者首轮每组输入哈希一致。后续轮次因模型所选胜者不同而分叉。", "",
              "| 页面 | Jev | Laya MLX | SemIf |", "|---|---|---|---|"]
    for i,(task,title) in enumerate(tasks):
        lines.append("| "+title.split(" → ")[0]+" | "+" | ".join(probes[n]["full_first_pages"][i]["selected"] for n in names)+" |")
    lines += ["", "没有最优下一跳标签，不根据文章标题主观判定这一部分的对错。", "",
              "## 完整三题", "", "每格为结果、点击数、动作时间。模型加载和网页初始设置单独记录，不计入 3,600 秒动作预算。", "",
              "| 任务 | Jev | Laya MLX | SemIf |", "|---|---|---|---|"]
    for task,title in tasks:
        cells = []
        for name in names:
            row = episodes.get((name,task))
            if row is None:
                cells.append("用户暂停；未完成" if pause.get("status") == "paused" and
                             pause.get("brain") == name and pause.get("task_id") == task else "运行中 / 待运行")
            else:
                status = "成功" if row["status"] == "success" else ("超时" if row["reason"] == "timeout" else "错误 / 未完成")
                cells.append(f"{status}；{row['clicks']}；{row['seconds']:.3f} 秒")
        lines.append("| "+title+" | "+" | ".join(cells)+" |")
    if summary["complete"]:
        lines += ["", "最终成功数："+"，".join(f"{labels[n]} {sum(e['status']=='success' for (b,_),e in episodes.items() if b==n)}/3" for n in names)+"。"]
    attempts = summary.get("archived_attempts", [])
    if attempts:
        lines += ["", "以上表格为每题当前保留的最后一次运行；以下失败尝试另行保留，其耗时没有计入上表：", "",
                  "| 后端 / 任务 | 原因 | 点击 | 动作耗时 |", "|---|---|---:|---:|"]
        for attempt in attempts:
            reason = attempt["reason"].replace("\n", " ").replace("|", "/")
            lines.append(f"| {attempt['brain']} / {attempt['task_id']} | {reason} | {attempt['clicks']} | {attempt['seconds']:.3f} 秒 |")
    lines += ["", "30 分钟未完成时沿同一轨迹继续到 60 分钟，不重新开局；无点击数上限。",
              "完整比赛读取实时网页，路线和遇到的候选会不同；固定输入实验才提供严格相同的证据。",
              "这些是三个任务各一次的系统对照，不足以估计稳定成功率或纯模型权重的差异。", "",
              "## 接口异常、复跑与证据", "",
              "首轮完整比赛中，Jev 的 Music 和 WWII 遇到 HTTP 400，Laya 的后续运行被主动中断以排查。",
              "逐组重放两个出错页面均成功，随后完整复跑 Jev 三题也全部成功，400 未再次复现。",
              "因此未把原因归为输入超限，也未根据这两次接口错误判断模型选路能力。原始试跑保留在",
              "`runs/unified-255/2026-09-21-v1/`，复跑在 `runs/unified-255/jev-http-diagnosis/`；没有删除失败记录。", "",
              "随后 v2 的 SemIf 在 Manipur 页触发编码一致性错误：Transformers 的运行时预分词器",
              "与原始 tokenizer.json 对梵文切分不同。第一题在 7 次点击后结束；第二题被主动中断排查。",
              "这属于适配错误，原日志保留在 `runs/unified-255/2026-09-21-v2/`，不作为模型能力结果。",
              "已把共享预算规划器改为使用实际运行时的预分词器，并验证 Manipur 全部 505 项编码一致。",
              "之前固定实验的每个调用也已重新审计，候选内容、分组与 token 审计均未改变。",
              "验证记录见 [Unicode 修复检查](2026-09-21-unified-unicode-fix.json)。",
              "固定输入实验后的运行时代码变化包括 HTTP 失败证据保存和预分词器一致性修复；",
              "模型规则、证据字段及推理读出方法保持一致。代码差异见 [修复记录](2026-09-21-unified-diagnostic-change.patch)。",
              "固定实验与完整比赛分别记录代码指纹；阶段内差异必须有独立修复审计，不能静默混用。",
              "本轮复用已重查的固定实验，并重新执行完整比赛；控制器再次核验协议和共享输入哈希。",
              "输入一致性等内部错误会停止派发；独立的网络/页面加载错误保存证据后继续其他任务。",
              "恢复执行会跳过已有成功或超时结果，先归档失败/中断证据再重跑相应任务，不删除旧记录。", "",
              f"本轮汇总目录：`{summary['root']}`。所有已保存原生调用均通过候选覆盖、规则/证据一致性与完整编码审计。",
              "[机器可读结果及审计](2026-09-21-unified-summary.json) 包含路径、逐阶段耗时、时间检查点和源文件位置。",
              "报告由后台测试流程更新；`complete` / `pending` 明确标示是否全部完成。", ""]
    if summary.get("runtime_fix_audit"):
        lines += ["## 网页 Unicode 截断修复", "",
                  "Laya 的 DNA 首次运行在 490 次点击、2,148.704 秒后，在量子力学页遇到 UTF-16 代理对被截断的错误。",
                  "该次未到 60 分钟，按程序中断单独记录；完整轨迹文件保存失败，仅逐步进度仍可用。DNA 从起点重跑。",
                  "修复只让网页截断避开半个数学字符，保持原来的长度预算；同时让异常诊断 JSON 能保存非法 Unicode。",
                  "修复前后六项已完成结果及固定输入文件的哈希均保留，真实量子力学页面已重放验证。",
                  "没有改模型、prompt、候选规则或推理方法。",
                  "验证记录见 [代理对截断审计](2026-09-21-unified-surrogate-fix.json)。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    text=render(json.loads(args.summary.read_text()))
    temporary=args.out.with_suffix('.md.tmp')
    temporary.write_text(text)
    temporary.replace(args.out)


if __name__ == "__main__":
    main()
