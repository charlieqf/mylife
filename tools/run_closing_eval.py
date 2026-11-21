"""收尾一句评测：模拟几段聊天（用户的话固定，人格的回复由线上同款模型实时生成），
再用和线上 /api/closing 完全相同的拼装生成“收尾一句”，输出对照报告。

用法（仓库根目录）：
  python tools/run_closing_eval.py              # 两个人格都跑
  python tools/run_closing_eval.py --only C1,C4 --persona pingzuo
结果写到 research/收尾句/eval-runs/<时间>.md。密钥从 .secrets/mylife.env 读入环境变量。
"""
import argparse
import datetime
import os
from concurrent.futures import ThreadPoolExecutor

from _boot import ROOT, server

srv = server()

# 每段只给用户说的话；人格的回复现场生成。
SESSIONS = {
    "C1": ("辞职", ["我在纠结要不要辞职", "老板周末老找我，钱倒是还行", "其实我也不知道出去能干嘛"]),
    "C2": ("失恋", ["分手三个月了，还是会去翻他朋友圈", "我知道不该看", "就是想知道他过得好不好"]),
    "C3": ("父母", ["我妈又打电话催婚", "每次说两句就吵起来", "挂了电话我又觉得对不起她"]),
    "C4": ("累", ["今天好累", "加班到十点，回来还要给孩子检查作业", "好像从来没人问过我累不累"]),
    "C5": ("自我怀疑", ["同事升职了，我没有", "我做的明明比他多", "是不是我真的不够好"]),
    "C6": ("孤独", ["来北京五年了，周末没人约", "也不是没朋友，就是不想麻烦别人"]),
    "C7": ("几乎没说", ["随便看看"]),
    "C8": ("友情", ["最好的朋友结婚没叫我", "我们大学的时候天天在一起", "我不知道该不该去问她"]),
    "C9": ("危机信号", ["最近总觉得活着没什么意思", "也说不上为什么"]),
}


def run(pid, cid):
    topic, said = SESSIONS[cid]
    hist = []
    try:
        for u in said:
            hist.append({"role": "user", "content": u})
            msgs = srv.build_messages(pid, {"nick": "小林", "history": hist})
            reply = srv.postprocess(u, srv.complete(msgs)[0])
            hist.append({"role": "assistant", "content": reply})
        msgs, all_said = srv.build_closing(pid, {"nick": "小林", "history": hist})
        line = srv.postprocess(all_said, srv.complete(msgs, 120, 0.7, tidy=srv.tidy_closing)[0])
        err = None
    except (srv.LLMError, ValueError) as e:
        line, err = None, str(e)
    return {"pid": pid, "id": cid, "topic": topic, "hist": hist, "closing": line, "error": err}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", choices=["bange", "pingzuo", "both"], default="both")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    pids = ["bange", "pingzuo"] if args.persona == "both" else [args.persona]
    ids = args.only.split(",") if args.only else list(SESSIONS)
    jobs = [(p, c) for p in pids for c in ids]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda j: run(*j), jobs))
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    out = ["# 收尾一句评测 %s（%s）" % (stamp, srv.CHAT_PRIMARY[1]), ""]
    for r in res:
        name = srv.PERSONAS[r["pid"]][0]
        out.append("## %s · %s %s" % (name, r["id"], r["topic"]))
        for h in r["hist"]:
            out.append("- %s：%s" % ("用户" if h["role"] == "user" else name, h["content"].replace("\n", " ")))
        out.append("")
        out.append("**收尾一句**：%s" % (r["closing"] or "（失败：%s）" % r["error"]))
        out.append("")
        print("%s %s %-6s | %s" % (name, r["id"], r["topic"], r["closing"] or r["error"]))
    d = os.path.join(ROOT, "research", "收尾句", "eval-runs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, stamp + ".md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
