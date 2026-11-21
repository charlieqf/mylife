"""长期记忆演示：模拟一个用户分几次聊天，看两个“另一个你”积累下来的了解，以及第 N 次聊天时的回复。

用法（仓库根目录）：python tools/memory_demo.py
用线上同款模型；记忆写到 .eval-cache/users/<临时 id>/，跑完不删，可以打开看 self.json。
"""
import os
import secrets
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, ".secrets", "mylife.env"), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)
os.environ["MYLIFE_PERSONAS"] = os.path.join(ROOT, "personas")
os.environ["MYLIFE_STYLE"] = os.path.join(ROOT, "prompts", "de-ai-style.txt")
os.environ["MYLIFE_SELF_FRAME"] = os.path.join(ROOT, "prompts", "self-frame.txt")
os.environ["MYLIFE_CLOSING_STYLE"] = os.path.join(ROOT, "prompts", "closing-style.txt")
os.environ.setdefault("MYLIFE_CACHE", os.path.join(ROOT, ".eval-cache"))
sys.path.insert(0, os.path.join(ROOT, "server"))
from memory import Memory  # noqa: E402
import mylife_server as srv  # noqa: E402

TALKS = [
    ("pingzuo", ["今天又加班到十点", "回来还要给女儿检查作业，她叫朵朵，上二年级", "我老婆出差了，家里就我一个大人", "说实话我有点撑不住，但又不能跟谁说"]),
    ("bange", ["我在想要不要从现在这家公司走", "干了六年了，老板人不坏，就是周末老找我", "我怕出去了找不到这么稳定的", "其实我最怕的是朵朵觉得爸爸老是不在家"]),
    ("pingzuo", ["朵朵今天考了一百分，我居然没赶上她的家长会", "她说没关系，可我听着心里难受"]),
]
LAST = ("bange", "老板又让我周六去公司，我该去吗")


def llm(msgs, max_tokens, temperature):
    return srv.llm_call(srv.CHAT_PRIMARY[0], srv.CHAT_PRIMARY[1], msgs, max_tokens=max_tokens, temperature=temperature, timeout=40)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    mem = Memory(os.path.join(os.environ["MYLIFE_CACHE"], "users"), llm)
    uid = "demo" + secrets.token_hex(8)
    for pid, said in TALKS:
        hist = []
        for u in said:
            hist.append({"role": "user", "content": u})
            know = mem.context(uid, u)
            reply = srv.postprocess(pid, u, srv.llm_call(srv.CHAT_PRIMARY[0], srv.CHAT_PRIMARY[1], srv.build_messages(pid, {"nick": "老陈", "history": hist}, know)))
            hist.append({"role": "assistant", "content": reply})
            mem.append(uid, pid, [("user", u), ("ai", reply)])
            print("  他：%s\n  %s：%s" % (u, srv.PERSONAS[pid][0], reply.replace("\n", " ")))
        r = mem.consolidate(uid)
        print("== 整理：新记住 %s；这次：%s\n" % (r and r["add"], r and r["episode"]))
    s = mem.summary(uid)
    print("== 认识第 %d 天，成形度 %d/5，记得 %d 件事" % (s["days"], s["level"], len(s["notes"])))
    print("画像：", s["portrait"])
    for n in s["notes"]:
        print(" -", n["text"])
    pid, u = LAST
    know = mem.context(uid, u)
    reply = srv.llm_call(srv.CHAT_PRIMARY[0], srv.CHAT_PRIMARY[1], srv.build_messages(pid, {"nick": "老陈", "history": [{"role": "user", "content": u}]}, know))
    print("\n== 新的一次（%s）\n  他：%s\n  %s：%s" % (srv.PERSONAS[pid][0], u, srv.PERSONAS[pid][0], reply))
    print("\n记忆文件：", os.path.join(os.environ["MYLIFE_CACHE"], "users", uid))


if __name__ == "__main__":
    main()
