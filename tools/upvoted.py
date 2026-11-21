"""列出被点赞/点踩的回复，作为下一轮提炼“收尾一句”和风格示例的素材。

用法（仓库根目录）：
  python tools/upvoted.py            # 先拉一次服务器存档（tools/chatlog.py pull），看最近 30 天
  python tools/upvoted.py --days 7 --no-pull
同一条回复以最后一次操作为准（点了又取消 = 不算）。结果写到 research/收尾句/点赞回复-<日期>.md。
"""
import argparse
import datetime
import os

from _boot import ROOT
from chatlog import pull, records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-pull", action="store_true")
    args = ap.parse_args()
    if not args.no_pull:
        pull()
    last = {}
    for r in records(args.days):
        if r["kind"] == "fb":
            last[(r.get("u") or r.get("ip"), r.get("mid"))] = r
    up, down = {}, {}
    for r in last.values():
        if r.get("v") in (1, -1):
            box = up if r["v"] == 1 else down
            k = (r.get("p"), r.get("reply"))
            box.setdefault(k, {"n": 0, "user": r.get("user", "")})["n"] += 1
    since = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()
    out = ["# 点赞回复（%s 起，共 %d 条被赞，%d 条被踩）" % (since, len(up), len(down)), ""]
    for title, box in (("被点赞", up), ("被点踩", down)):
        out.append("## " + title)
        for (p, reply), v in sorted(box.items(), key=lambda kv: -kv[1]["n"]):
            out.append("- [%s ×%d] 用户：%s\n  回复：%s" % (p, v["n"], v["user"], (reply or "").replace("\n", " ")))
        out.append("")
    d = os.path.join(ROOT, "research", "收尾句")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "点赞回复-%s.md" % datetime.date.today().isoformat())
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("%d upvoted, %d downvoted -> %s" % (len(up), len(down), path))


if __name__ == "__main__":
    main()
