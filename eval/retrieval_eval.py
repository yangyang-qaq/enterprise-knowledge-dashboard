"""文件级召回 A/B 评测：不开图 vs 开图，直接在 golden_set 的 gold_files 上算指标。

不需要 LLM（秒级~分钟级），因为 ground truth 就是「哪些文档能回答这个问题」——
这是 golden_set 里可判的字段，不用人工标注，也不用 judge。

用法：
  python retrieval_eval.py --verify-golden-set          # 先验 ground truth 是否属实
  python retrieval_eval.py --mode compare               # 跑 A/B，出报告
  python retrieval_eval.py --mode compare --compare     # 顺带做回归门（Δ<0 且超容差 → 退出码 1）
  python retrieval_eval.py --mode compare --set-config GRAPH_HOPS=2 --set-config GRAPH_HUB_DEGREE_CAP=5

## 为什么默认 K=3 而不是 RETRIEVAL_K=10

这是本脚本最重要的一条设计约束。只有 4~6 个文档的语料上，base 臂在 k>=5 就基本
覆盖全集，文件级 recall 已经摸到天花板 —— 图再加新文件也涨不动，Δ 必然是 0。
实测（6 个 query × 5 个 k，base/graph 各跑一次完整检索）：

    k=1  4/6 个 query 文件级变好
    k=2  2/6
    k=3  2/6
    k=5  1/6      <- 指标饱和区
    k=10 3/6

同时 `graph_added` 在 30 组里 28 组都是 2 —— 图**一直在工作**，只是大 k 下没有
「新文件」可加。所以：

- 文件级 Δ 必须在**小 K** 上看，K=5/10 会得出「图谱没用」的错误结论；
- 报告里必须同时给 `graph_added`，它才是「图在工作」的直接证据；
- 想在大 K 上也有文件级提升，唯一的办法是**加语料**（文档多了 base 覆盖不满）。

## 分层报告

`tier=cross_doc`（gold_files >= 2）与 `tier=single_doc` 分开报。全量会被单文档题
稀释，跨文档子集才是图谱机制该发力的地方。
"""

import argparse
import json
import os
import sys
import time

import requests

import config
from retrieve import retrieve_chunks, signin


# ── ground truth 自检 ──
def load_kb_docs(kb_id: str, token: str) -> dict[str, str]:
    """从 KB 的 chunk 里拼出「文件名 -> 全文」，用于校验 gold_files 是否属实。

    走 /evaluate/query 拿不到全文，所以这里直接读 Chroma：/graph/search 之类
    都不合适。最省事的是用 knowledge 的 files 接口 —— 但那条路要 file 详情权限，
    这里用最简单可靠的 /knowledge/{id}/files 列出文件，再逐条取内容。
    """
    r = requests.get(
        f"{config.BACKEND_URL}{config.API}/knowledge/{kb_id}/files",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    docs: dict[str, str] = {}
    # 这系列列表接口返回的是 {"items": [...]} 不是裸数组——直接迭代 dict 会拿到
    # 字符串键，然后静默产出一个空的 docs，校验就变成「全部通过」。
    for f in r.json().get("items", []):
        fid = f.get("id")
        if not fid:
            continue
        rc = requests.get(
            f"{config.BACKEND_URL}{config.API}/files/{fid}/content",
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        if rc.ok:
            docs[f.get("filename") or fid] = rc.text
    return docs


def verify_golden_set(entries: list[dict], kb_id: str, token: str) -> int:
    """逐条检查 ``evidence`` 的每个子串是否真的出现在它声明的文件里。

    这份 golden_set 的 gold_files 是人工判定的，判定错了整个 A/B 就没有意义
    —— 而且是那种「跑出来数字很好看但根本没测到东西」的错。所以先机械校验一遍。
    """
    docs = load_kb_docs(kb_id, token)
    if not docs:
        print("FAIL: 取不到 KB 文件内容，无法校验 golden_set", file=sys.stderr)
        return 1
    print(f"KB 文件数: {len(docs)} -> {sorted(docs)}")
    bad = 0
    for e in entries:
        for fname, needle in (e.get("evidence") or {}).items():
            if fname not in docs:
                print(f"  FAIL {e['id']}: gold_files 里的 {fname} 不在 KB 里")
                bad += 1
            elif needle not in docs[fname]:
                print(f"  FAIL {e['id']}: {fname} 中找不到证据串 {needle!r}")
                bad += 1
        if e.get("tier") == "cross_doc" and len(e.get("gold_files") or []) < 2:
            print(f"  FAIL {e['id']}: tier=cross_doc 但 gold_files < 2")
            bad += 1
    n = len(entries)
    print(f"校验 {n} 条：{n - bad} 通过，{bad} 失败")
    return 1 if bad else 0


# ── 指标 ──
def score_entry(entry: dict, chunks: list[dict], k: int) -> dict:
    """文件级指标。只看 top-k 前缀——图谱扩展会把结果撑到 k 条以上。"""
    gold = set(entry.get("gold_files") or [])
    topk = [c for c in chunks if not c.get("beyond_k")]
    retrieved = {c["source"] for c in topk if c.get("source")}
    hit = gold & retrieved
    mrr = 0.0
    for c in topk:
        if c.get("source") in gold:
            mrr = 1.0 / max(1, c.get("rank") or 1)
            break
    return {
        "recall": round(len(hit) / len(gold), 4) if gold else 0.0,
        "hit": 1.0 if hit else 0.0,
        "mrr": round(mrr, 4),
        "gold_hit": sorted(hit),
        "gold_missed": sorted(gold - retrieved),
        "retrieved_files": sorted(retrieved),
        "graph_added": sum(1 for c in chunks if c.get("from_graph")),
        "graph_added_topk": sum(1 for c in topk if c.get("from_graph")),
        "n_chunks": len(chunks),
    }


def mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def summarise(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "recall_at_k": mean([r["recall"] for r in rows]),
        "hit_at_k": mean([r["hit"] for r in rows]),
        "mrr": mean([r["mrr"] for r in rows]),
        "graph_added": mean([r["graph_added"] for r in rows]),
        "graph_added_topk": mean([r["graph_added_topk"] for r in rows]),
    }


def run_arm(entries: list[dict], kb_id: str, token: str, k: int, use_graph: bool) -> dict:
    per_entry = []
    for e in entries:
        chunks = retrieve_chunks(e["question"], kb_id, token, k=k, use_graph=use_graph)
        s = score_entry(e, chunks, k)
        s["id"] = e["id"]
        s["tier"] = e.get("tier", "single_doc")
        per_entry.append(s)
        print(
            f"  [{'graph' if use_graph else 'base '}] {e['id']} recall={s['recall']:.2f} "
            f"graph_added={s['graph_added']} 命中 {s['gold_hit']} 漏 {s['gold_missed']}"
        )
    return {
        "overall": summarise(per_entry),
        "cross_doc": summarise([r for r in per_entry if r["tier"] == "cross_doc"]),
        "single_doc": summarise([r for r in per_entry if r["tier"] == "single_doc"]),
        "entries": per_entry,
    }


# ── 后端配置临时改写（灵敏度检查用）──
def set_backend_config(pairs: list[str], token: str) -> dict:
    """""KEY=VALUE" 形式批量改后端 rag 配置，返回改动前的旧值用于回滚。

    灵敏度检查必须走真配置而不是脚本内部参数：要证明的是「评测能感知到真实
    参数变化」，如果只在脚本里改个变量，那证明不了任何事。
    """
    r = requests.get(
        f"{config.BACKEND_URL}{config.API}/retrieval/config",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    before_all = r.json()
    payload = {}
    before = {}
    for p in pairs:
        key, _, val = p.partition("=")
        key = key.strip()
        raw = val.strip()
        # 按旧值类型还原：bool / int / str
        old = before_all.get(key)
        before[key] = old
        if isinstance(old, bool):
            payload[key] = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(old, int):
            payload[key] = int(raw)
        else:
            payload[key] = raw
    ru = requests.post(
        f"{config.BACKEND_URL}{config.API}/retrieval/config/update",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    ru.raise_for_status()
    print(f"  已改后端配置 {payload}（旧值 {before}）")
    return before


def restore_backend_config(before: dict, token: str) -> None:
    if not before:
        return
    requests.post(
        f"{config.BACKEND_URL}{config.API}/retrieval/config/update",
        json=before,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    print(f"  已还原后端配置 {before}")


# ── 报告 ──
def write_report(report: dict, report_dir: str) -> tuple[str, str]:
    os.makedirs(report_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = os.path.join(report_dir, f"retrieval-graph-{stamp}.json")
    md_path = os.path.join(report_dir, f"retrieval-graph-{stamp}.md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    base, graph = report["base"], report["graph"]
    lines = [
        "# 图谱检索 A/B 报告（文件级召回）",
        "",
        f"- KB: `{report['kb_id']}`",
        f"- K = **{report['k']}**（小 K；大 K 下文件级指标会饱和，见脚本 docstring）",
        f"- 题目数: {report['n_entries']}（跨文档 {report['n_cross_doc']} / 单文档 {report['n_single_doc']}）",
        f"- 后端配置: `{json.dumps(report.get('config'), ensure_ascii=False)}`",
        "",
        "## 总览",
        "",
        "| 子集 | 臂 | recall@K | hit@K | MRR | graph_added |",
        "|---|---|---|---|---|---|",
    ]
    for subset, label in (("overall", "全量"), ("cross_doc", "跨文档"), ("single_doc", "单文档")):
        for arm, name in ((base, "base"), (graph, "graph")):
            m = arm[subset]
            lines.append(
                f"| {label} | {name} | {m['recall_at_k']} | {m['hit_at_k']} | {m['mrr']} | {m['graph_added']} |"
            )
        d = round(graph[subset]["recall_at_k"] - base[subset]["recall_at_k"], 4)
        lines.append(f"| {label} | **Δ** | **{d:+}** | | | |")

    lines += ["", "## 逐题对比", "", "| id | tier | base recall | graph recall | graph_added | 图新增的 gold 文件 |", "|---|---|---|---|---|---|"]
    by_id = {r["id"]: r for r in base["entries"]}
    for g in graph["entries"]:
        b = by_id.get(g["id"], {})
        new = sorted(set(g["gold_hit"]) - set(b.get("gold_hit") or []))
        lines.append(
            f"| {g['id']} | {g['tier']} | {b.get('recall')} | {g['recall']} | {g['graph_added']} | {', '.join(new) or '-'} |"
        )

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return json_path, md_path


def main() -> None:
    ap = argparse.ArgumentParser(description="图谱检索文件级召回 A/B 评测")
    ap.add_argument("--mode", choices=["compare"], default="compare")
    ap.add_argument("--golden-set", default=os.path.join(config.BASE_DIR, "golden_set_demo_kb.json"))
    ap.add_argument("--kb-id", default=None, help="缺省取 golden_set 里第一条的 kb_id")
    ap.add_argument("--k", type=int, default=config.GRAPH_EVAL_K)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--verify-golden-set", action="store_true", help="只校验 ground truth 是否属实，不跑检索")
    ap.add_argument("--set-config", action="append", default=[], help="临时代码改后端配置 KEY=VALUE，跑完还原")
    ap.add_argument("--compare", action="store_true", help="回归门：跨文档子集 Δ 低于 -容差 → 退出码 1")
    ap.add_argument("--tolerance", type=float, default=config.REGRESSION_TOLERANCE)
    ap.add_argument("--report-dir", default=config.REPORT_DIR)
    args = ap.parse_args()

    with open(args.golden_set, "r", encoding="utf-8") as f:
        entries = json.load(f)
    if args.limit:
        entries = entries[: args.limit]
    kb_id = args.kb_id or (entries[0].get("kb_id") if entries else None) or config.DEFAULT_KB_ID
    if not kb_id:
        print("FAIL: 没有 kb_id（golden_set 里没有，也未传 --kb-id / EVAL_KB_ID）", file=sys.stderr)
        sys.exit(1)

    token = signin()

    if args.verify_golden_set:
        sys.exit(verify_golden_set(entries, kb_id, token))

    before = {}
    try:
        if args.set_config:
            before = set_backend_config(args.set_config, token)

        print(f"跑 base 臂（K={args.k}, {len(entries)} 题）...")
        base = run_arm(entries, kb_id, token, args.k, use_graph=False)
        print(f"跑 graph 臂（K={args.k}, {len(entries)} 题）...")
        graph = run_arm(entries, kb_id, token, args.k, use_graph=True)
    finally:
        restore_backend_config(before, token)

    report = {
        "kb_id": kb_id,
        "k": args.k,
        "n_entries": len(entries),
        "n_cross_doc": sum(1 for e in entries if e.get("tier") == "cross_doc"),
        "n_single_doc": sum(1 for e in entries if e.get("tier") != "cross_doc"),
        "config": args.set_config or "（默认）",
        "base": base,
        "graph": graph,
    }
    json_path, md_path = write_report(report, args.report_dir)

    def d(subset: str) -> float:
        return round(graph[subset]["recall_at_k"] - base[subset]["recall_at_k"], 4)

    print("\n=== 文件级 recall@K ===")
    for subset, label in (("overall", "全量"), ("cross_doc", "跨文档"), ("single_doc", "单文档")):
        print(
            f"  {label:6} base={base[subset]['recall_at_k']:.4f} "
            f"graph={graph[subset]['recall_at_k']:.4f}  Δ={d(subset):+.4f}  "
            f"graph_added(均值)={graph[subset]['graph_added']:.2f}"
        )
    print(f"\n报告: {json_path}\n      {md_path}")

    if args.compare:
        if d("cross_doc") < -args.tolerance:
            print(f"FAIL: 跨文档子集 Δ={d('cross_doc'):+.4f} 低于 -容差 {args.tolerance}")
            sys.exit(1)
        print(f"PASS: 跨文档子集 Δ={d('cross_doc'):+.4f}（容差 -{args.tolerance}）")


if __name__ == "__main__":
    main()
