"""对话存档：从服务器拉下来，看统计、按人读整段对话、拿真实对话用现在的提示词重放。

用法（仓库根目录）：
  python tools/chatlog.py pull                         # 服务器 /var/lib/mylife（logs/ users/ legacy/）→ data/server/
  python tools/chatlog.py stats [--days 7]             # 每天：轮数、人数、回复长度、截断、点赞点踩、评分、失败
  python tools/chatlog.py convo [UID前缀] [--days 30]   # 不给前缀：列出最近的人；给前缀：整段对话写成 markdown
  python tools/chatlog.py replay [--days 7] [--p bange] [--n 20] [--min-len 20]
                                                       # 存档里的真实输入（历史 + 记忆块）用现在的提示词再生成一遍，新旧对照
data/ 不进仓库（.gitignore）。报告写到 data/reports/。服务器地址从 .secrets/deploy.env 读取。
存档格式见 server/mylife_server.py 开头：每行一条，kind = chat/open/closing/card/fb/local/rating/asr/error。
"""
import argparse
import collections
import datetime
import glob
import io
import json
import os
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor

from _boot import ROOT, read_env

DATA = os.path.join(ROOT, "data", "server")
REPORTS = os.path.join(ROOT, "data", "reports")


def ssh_cmd(remote):
    env = {k: os.path.expandvars(v.replace("$HOME", os.path.expanduser("~"))) for k, v in read_env("deploy.env").items()}
    return ["ssh", "-p", env.get("MYLIFE_DEPLOY_PORT", "22"), "-i", env["MYLIFE_DEPLOY_KEY"], "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes", env["MYLIFE_DEPLOY_HOST"], remote]


def pull():
    """Mirror the server's archive and memory into data/server (whole tree each time; it is small)."""
    raw = subprocess.run(ssh_cmd("cd /var/lib/mylife && tar -czf - $(ls -d logs users legacy 2>/dev/null)"),
                         capture_output=True, check=True).stdout
    os.makedirs(DATA, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(DATA, filter="data")
    n = len(glob.glob(os.path.join(DATA, "logs", "*.jsonl")))
    print("pulled -> %s (%d day files, %d users)" % (DATA, n, len(glob.glob(os.path.join(DATA, "users", "*")))))


def records(days=None):
    """Every archived record, oldest first. Old-format logs (before 2026-11-22) are read too: chat records without
    u/tid, feedback records without kind."""
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat() if days else ""
    files = [(f, None) for f in glob.glob(os.path.join(DATA, "logs", "*.jsonl"))]
    files += [(f, None) for f in glob.glob(os.path.join(DATA, "legacy", "chatlog", "*.jsonl"))]
    files += [(f, "fb") for f in glob.glob(os.path.join(DATA, "legacy", "feedback", "*.jsonl"))]
    out = []
    for f, kind in files:
        if os.path.basename(f)[:10] < since:
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                r.setdefault("kind", kind)
                out.append(r)
    return sorted(out, key=lambda r: r.get("t", ""))


def who(r):
    return r.get("u") or "ip:" + str(r.get("ip"))


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0


def stats(args):
    days = collections.defaultdict(list)
    for r in records(args.days):
        days[r.get("t", "")[:10]].append(r)
    print("日期        人数 对话轮 半格/平坐  平均字数 截断 p50/p90秒  开口 收尾 卡  👍/👎  评分(n)  脚本回复  语音 失败")
    for d in sorted(days):
        rs = days[d]
        k = collections.Counter(r["kind"] for r in rs)
        chat = [r for r in rs if r["kind"] == "chat"]
        ms = [r.get("ms", 0) / 1000 for r in chat]
        fb = [r for r in rs if r["kind"] == "fb"]
        rating = [r["rating"] for r in rs if r["kind"] == "rating" and r.get("rating")]
        local = collections.Counter(r.get("why") for r in rs if r["kind"] == "local")
        print("%s %4d %6d %4d/%-4d %8.0f %4d %5.1f/%-5.1f %4d %4d %2d %3d/%-3d %5s(%d)  %-9s %4d %4d" % (
            d, len({who(r) for r in rs if r["kind"] in ("chat", "local")}), len(chat),
            sum(r["p"] == "bange" for r in chat), sum(r["p"] == "pingzuo" for r in chat),
            sum(len(r.get("reply", "")) for r in chat) / max(1, len(chat)), sum("raw" in r for r in chat),
            pct(ms, .5), pct(ms, .9), k["open"], k["closing"], k["card"],
            sum(r.get("v") == 1 for r in fb), sum(r.get("v") == -1 for r in fb),
            "%.1f" % (sum(rating) / len(rating)) if rating else "-", len(rating),
            ",".join("%s%d" % kv for kv in local.items()) or "-", k["asr"], k["error"]))


LABEL = {"chat": "", "open": "（先开口）", "closing": "（收尾一句）", "card": "（彩虹卡）", "local": "（页面固定回复）"}


def paras(text):
    return "\n\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())


def typed(r):
    """What he actually typed (chat records keep the model-facing line in `user`, the raw one in `hist`)."""
    last = next((h.get("content") for h in reversed(r.get("hist") or []) if h.get("role") == "user"), None)
    return last or r.get("user")


def convo(args):
    rs = [r for r in records(args.days) if r["kind"] != "asr"]
    if not args.prefix:
        people = collections.defaultdict(list)
        for r in rs:
            people[who(r)].append(r)
        for w, l in sorted(people.items(), key=lambda kv: kv[1][-1]["t"], reverse=True)[:30]:
            print("%-16s %-8s %3d 条  %s → %s" % (w[:16], l[-1].get("nick") or "", len(l), l[0]["t"][5:16], l[-1]["t"][5:16]))
        return
    mine = [r for r in rs if who(r).startswith(args.prefix)]
    if not mine:
        sys.exit("no records for %s" % args.prefix)
    lines, day = ["# 对话存档 · %s（%s）" % (who(mine[0]), mine[-1].get("nick") or ""), ""], ""
    for r in mine:
        if r["t"][:10] != day:
            day = r["t"][:10]
            lines += ["## " + day, ""]
        name, t = {"bange": "半格", "pingzuo": "平坐"}.get(r.get("p"), r.get("p")), r["t"][11:16]
        if r["kind"] in LABEL:
            last = typed(r) if r["kind"] in ("chat", "local") else None
            if last:
                lines += ["**%s 他：** %s" % (t, paras(last)), ""]
            lines.append("**%s %s%s：** %s%s" % (t, name, LABEL[r["kind"]], paras(r.get("reply")),
                                              "　〔%d字%s，%s〕" % (len(r.get("reply", "")), "，截断" if "raw" in r else "",
                                                                 r.get("pv", "")) if r["kind"] == "chat" else ""))
        elif r["kind"] == "fb":
            lines.append("> %s %s %s" % (t, {1: "👍", -1: "👎", 0: "取消"}.get(r.get("v")), " ".join(str(r.get("reply") or "").split())[:40]))
        elif r["kind"] == "rating":
            lines.append("> %s 结束对话，评分 %s%s" % (t, r.get("rating"), "：" + r["note"] if r.get("note") else ""))
        elif r["kind"] == "error":
            lines.append("> %s 模型出错：%s" % (t, r.get("error")))
        lines.append("")
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, "对话-%s.md" % who(mine[0]).replace(":", "-")[:16])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("%d records -> %s" % (len(mine), path))


def replay(args):
    from _boot import server
    srv = server()
    picks = [r for r in records(args.days) if r["kind"] == "chat" and r.get("hist") and (not args.p or r["p"] == args.p)
             and len(r.get("user", "")) >= args.min_len][-args.n:]
    if not picks:
        sys.exit("no replayable chat records (need the new format with hist; run `pull` first)")

    def one(r):
        users = [h.get("content", "") for h in r["hist"][-6:] if h.get("role") == "user"]
        vids = srv.pick_views(r["p"], users, lambda m: srv.complete(m, 20, 0, 8)[0])
        msgs = srv.build_messages(r["p"], {"nick": r.get("nick") or "朋友", "history": r["hist"]}, r.get("know"),
                                  srv.views_block(r["p"], vids))
        text, model = srv.complete(msgs, srv.CHAT_MAX_TOKENS, timeout=40)
        return srv.postprocess(msgs[-1]["content"], srv.fit(text, r.get("cap") or srv.REPLY_MAX))

    with ThreadPoolExecutor(4) as pool:
        new = list(pool.map(one, picks))
    out = ["# 重放：存档里的真实输入 × 现在的提示词（%s）" % datetime.datetime.now().isoformat(timespec="minutes"), ""]
    for r, n in zip(picks, new):
        name = {"bange": "半格", "pingzuo": "平坐"}[r["p"]]
        out += ["## %s · %s · %s" % (r["t"][:16], name, who(r)[:10]), "", "**他：** " + paras(typed(r)), "",
                "**当时（%s，%d字）：**" % (r.get("pv", "?"), len(r["reply"])), "", r["reply"], "",
                "**现在（%s，%d字）：**" % (srv.prompt_version(r["p"]), len(n)), "", n, "", "---", ""]
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, "重放-%s.md" % datetime.datetime.now().strftime("%Y%m%d-%H%M"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("%d turns replayed -> %s" % (len(picks), path))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("pull")
    a = sp.add_parser("stats")
    a.add_argument("--days", type=int, default=7)
    a = sp.add_parser("convo")
    a.add_argument("prefix", nargs="?")
    a.add_argument("--days", type=int, default=30)
    a = sp.add_parser("replay")
    a.add_argument("--days", type=int, default=7)
    a.add_argument("--p", choices=["bange", "pingzuo"])
    a.add_argument("--n", type=int, default=20)
    a.add_argument("--min-len", type=int, default=20)
    args = ap.parse_args()
    {"pull": lambda a: pull(), "stats": stats, "convo": convo, "replay": replay}[args.cmd](args)


if __name__ == "__main__":
    main()
