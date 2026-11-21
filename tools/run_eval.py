"""人格评测：用和线上 /api/chat 完全相同的提示词拼装与模型调用，逐条跑评测集，输出对照报告。

用法（仓库根目录）：
  python tools/run_eval.py bange                 # 默认模型 = 线上主模型（deepseek-v4.1-flash，TokenHub，关思考）
  python tools/run_eval.py pingzuo --only P04,P08
  python tools/run_eval.py bange --endpoint dashscope --model qwen3.8-max

评测集：personas/<id>/评测集.jsonl，每行 {"id","cat","turns":[{"role","content"}...],"expect","avoid"}，
turns 最后一条必须是 user。结果写到 personas/<id>/eval-runs/<时间>-<模型>.md 和 .json。
密钥从 .secrets/mylife.env 读入环境变量，不写进代码。
"""
import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, ".secrets", "mylife.env"), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)
os.environ["MYLIFE_PERSONAS"] = os.path.join(ROOT, "personas")
os.environ["MYLIFE_STYLE"] = os.path.join(ROOT, "prompts", "de-ai-style.txt")
os.environ.setdefault("MYLIFE_CACHE", os.path.join(ROOT, ".eval-cache"))
sys.path.insert(0, os.path.join(ROOT, "server"))
import mylife_server as srv  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("persona", choices=sorted(srv.PERSONAS))
    ap.add_argument("--only", default="")
    ap.add_argument("--endpoint", default=srv.CHAT_PRIMARY[0])
    ap.add_argument("--model", default=srv.CHAT_PRIMARY[1])
    ap.add_argument("--nick", default="小林")
    ap.add_argument("--profile", default="")
    args = ap.parse_args()

    pdir = os.path.join(ROOT, "personas", args.persona)
    with open(os.path.join(pdir, "评测集.jsonl"), encoding="utf-8") as f:
        cases = [json.loads(l) for l in f if l.strip()]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]

    def run(c):
        req = {"nick": args.nick, "profile": args.profile, "history": c["turns"]}
        msgs = srv.build_messages(args.persona, req)
        t0 = datetime.datetime.now()
        try:
            reply, err = srv.postprocess(args.persona, msgs[-1]["content"],
                                         srv.llm_call(args.endpoint, args.model, msgs)), None
        except srv.LLMError as e:
            reply, err = None, str(e)
        return dict(c, reply=reply, error=err, secs=(datetime.datetime.now() - t0).total_seconds(),
                    system_chars=len(msgs[0]["content"]))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, cases))

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = os.path.join(pdir, "eval-runs")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, "%s-%s" % (stamp, args.model))
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({"model": args.model, "results": results}, f, ensure_ascii=False, indent=1)
    lines = ["# %s 评测 · %s · %s" % (args.persona, stamp, args.model), "",
             "%d 条；系统提示词约 %d 字。" % (len(results), results[0]["system_chars"] if results else 0), ""]
    for r in results:
        lines.append("## %s · %s" % (r["id"], r["cat"]))
        for t in r["turns"]:
            lines.append("- **%s**：%s" % ("用户" if t["role"] == "user" else "人格", t["content"]))
        lines.append("- **回复**：%s" % (r["reply"] if r["reply"] else "（错误）%s" % r["error"]))
        lines.append("- 期望：%s" % r.get("expect", ""))
        if r.get("avoid"):
            lines.append("- 不应：%s" % r["avoid"])
        lines.append("- 耗时 %.1fs" % r["secs"])
        lines.append("")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(base + ".md")


if __name__ == "__main__":
    main()
