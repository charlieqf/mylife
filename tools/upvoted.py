"""拉取服务器上的点赞/点踩记录，列出被点赞的回复，作为下一轮提炼“收尾一句”和风格示例的素材。

用法（仓库根目录，Git Bash 或 PowerShell 均可）：
  python tools/upvoted.py            # 最近 30 天
  python tools/upvoted.py --days 7
同一条回复以最后一次操作为准（点了又取消 = 不算）。结果写到 research/收尾句/点赞回复-<日期>.md。
服务器地址从 .secrets/deploy.env 读取（不进仓库）。
"""
import argparse
import datetime
import json
import os
import subprocess

from _boot import ROOT, read_env


def deploy_env():
    return {k: os.path.expandvars(v.replace("$HOME", os.path.expanduser("~"))) for k, v in read_env("deploy.env").items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    env = deploy_env()
    since = (datetime.date.today() - datetime.timedelta(days=args.days)).isoformat()
    cmd = ["ssh", "-p", env.get("MYLIFE_DEPLOY_PORT", "22"), "-i", env["MYLIFE_DEPLOY_KEY"], "-o", "IdentitiesOnly=yes",
           "-o", "BatchMode=yes", env["MYLIFE_DEPLOY_HOST"],
           "cd /var/cache/mylife/feedback 2>/dev/null && for f in *.jsonl; do [ \"${f%%.jsonl}\" \\> \"%s\" ] && cat \"$f\"; done; true" % since]
    raw = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True).stdout
    last = {}
    for line in raw.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        last[(r.get("ip"), r.get("mid"))] = r
    up, down = {}, {}
    for r in last.values():
        if r.get("v") in (1, -1):
            box = up if r["v"] == 1 else down
            k = (r.get("p"), r.get("reply"))
            box.setdefault(k, {"n": 0, "user": r.get("user", "")})["n"] += 1
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
