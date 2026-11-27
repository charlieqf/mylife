"""接着上次的事 + 红点留言（docs/方案_接着上次的事与红点留言.md）的检查。

    python tools/follow_eval.py           离线：桩模型、临时目录，不花钱
    python tools/follow_eval.py --live    再加真模型：回来接着聊、抽取、报结果、开场（全是合成对话）
"""
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import time

from _boot import server

srv = server()
import memory  # noqa: E402  (server() puts server/ on the path)
from memory import Memory  # noqa: E402

UID = "followevaluser000000000"
NOW = datetime.datetime.now()
TODAY = NOW.date()
D = lambda n: TODAY + datetime.timedelta(days=n)
ok = total = 0


def check(cond, what, note=""):
    global ok, total
    total += 1
    ok += bool(cond)
    print("%s %s %s" % ("PASS" if cond else "FAIL", what, ("| " + str(note)[:300]) if note != "" else ""), flush=True)
    return cond


def fresh(model=None, follow=True):
    """A Memory in a new temp dir; model(msgs) -> text (the fold's answer); calls are counted in .calls."""
    calls = []

    def llm(msgs, max_tokens, temperature):
        calls.append(msgs)
        return model(msgs) if callable(model) else model
    mem = Memory(tempfile.mkdtemp(prefix="follow-eval-"), llm, follow=follow, unsafe=srv.CRISIS.search)
    mem.calls = calls
    return mem


def seed(mem, rows):
    """Write his log as if said at those times: rows = [(datetime, pid, role, text)]; nothing folded yet."""
    os.makedirs(mem._dir(UID), exist_ok=True)
    with open(os.path.join(mem._dir(UID), "log.jsonl"), "a", encoding="utf-8") as f:
        for t, pid, role, text in rows:
            f.write(json.dumps({"t": t.isoformat(timespec="seconds"), "p": pid, "role": role, "text": text},
                               ensure_ascii=False) + "\n")
    prof = mem._load(UID)
    prof["msgs"] += sum(1 for r in rows if r[2] == "user")
    mem._save(UID, prof)


def put_follows(mem, follows, day=None):
    prof = mem._load(UID)
    prof["follows"], prof["follow_day"] = follows, day
    mem._save(UID, prof)


def follows(mem):
    return mem._load(UID)["follows"]


def fold_answer(follow=(), close=(), episode="你说了一件事"):
    return json.dumps({"episode": episode, "add": [], "drop": [], "portrait": "", "follow": list(follow),
                       "close": list(close)}, ensure_ascii=False)


def F(fid, due, p="bange", state="open", text=None):
    return {"id": fid, "text": text or "你有件事（%s）" % fid, "due": due.isoformat(), "p": p, "state": state}


def at(days, hour=21):
    return datetime.datetime.combine(D(days), datetime.time(hour, 0))


def app_with(mem, model=None):
    """A real mylife_server.App on this memory; model(msgs) -> text instead of the real one when given."""
    app = srv.App()
    app.log = lambda *a, **k: "tid"
    if model:
        app.llm = lambda msgs, max_tokens=300, temperature=0.8, timeout=25, tidy=str.strip: (model(msgs), "stub", 0)
        mem.llm = lambda msgs, mt, t: model(msgs)
    else:
        mem.llm = lambda msgs, mt, t: app.llm(msgs, mt, t, timeout=40)[0]
    app.mem = mem
    return app


# ---------------------------------------------------------------- offline
def offline():
    print("== 日期：周四说“明天面试”，周五不问，周六、周日、周一能问，周二作废")
    thu = D(0)
    prof = {"follows": [F("a", thu + datetime.timedelta(days=2))], "follow_day": None}
    seen = [bool(memory._follow_pick(prof, thu + datetime.timedelta(days=k))) for k in range(1, 6)]
    check(seen == [False, True, True, True, False], "周五 周六 周日 周一 周二 = 否 是 是 是 否", seen)

    print("== 整理：每段标日期，按说的那天算；记下的事归最后一句的那个自己")
    mem = fresh(fold_answer([{"text": "你明天下午有个面试", "due": D(1).isoformat()}]))
    seed(mem, [(at(-1), "pingzuo", "user", "明天下午有个面试，有点慌"), (at(-1), "pingzuo", "ai", "嗯，慌是正常的。")])
    mem.consolidate(UID, 1)
    sent = mem.calls[-1][1]["content"]
    check("【%s 周%s 说的】" % (D(-1).isoformat(), memory.WEEKDAYS[D(-1).weekday()]) in sent and "【今天】" + TODAY.isoformat() in sent,
          "给模型的聊天记录标着那天的日期，开头有今天", sent[:120])
    check("follow" in mem.calls[-1][0]["content"] and "【等着问的事】" in sent, "开着时提示词里有 follow 和【等着问的事】")
    fs = follows(mem)
    check(len(fs) == 1 and fs[0]["due"] == D(1).isoformat() and fs[0]["p"] == "pingzuo" and fs[0]["state"] == "open",
          "记下一条：due 对、归伙伴、open", fs)
    check(mem._load(UID)["episodes"][-1]["d"] == D(-1).isoformat(), "这次聊天记成昨天（最后一句那天），不是整理那天",
          mem._load(UID)["episodes"][-1])

    print("== 只说一句：/api/sync 的门槛 1 会整理，2 不会")
    for n, want in ((2, 0), (1, 1)):
        mem = fresh(fold_answer([{"text": "你后天下午去医院拿体检结果", "due": D(3).isoformat()}]))
        seed(mem, [(NOW, "bange", "user", "后天下午去医院拿体检结果，有点紧张"), (NOW, "bange", "ai", "嗯。")])
        mem.consolidate(UID, n)
        check(len(mem.calls) == want and len(follows(mem)) == want, "门槛 %d：模型调用 %d 次，记下 %d 条" % (n, want, want),
              (len(mem.calls), follows(mem)))
    check(follows(mem)[0]["due"] == D(3).isoformat() and mem.summary(UID)["due"] is None,
          "一句话记下了、日期对；当天 summary 的 due 是 null（还没到）", follows(mem))

    print("== 模型给错的不收")
    bad = [{"text": "你明天面试", "due": TODAY.isoformat()},                       # 不晚于说的那天
           {"text": "你明年体检", "due": D(40).isoformat()},                      # 超过 30 天
           {"text": "你说明天就去自杀", "due": D(2).isoformat()},                  # 危机话
           {"text": "你周一搬家", "due": "下周二"},                                # 不是日期
           {"text": "", "due": D(2).isoformat()}]
    mem = fresh(fold_answer(bad))
    seed(mem, [(NOW, "bange", "user", "随便说说")])
    mem.consolidate(UID, 1)
    check(follows(mem) == [], "日期不晚于说的那天、超 30 天、危机话、不是日期、空的：都不收", follows(mem))
    three = [{"text": "你事情%d" % i, "due": D(2 + i).isoformat()} for i in range(3)]
    mem = fresh(fold_answer(three + [{"text": "你事情0", "due": D(2).isoformat()}]))
    seed(mem, [(NOW, "bange", "user", "随便说说")])
    mem.consolidate(UID, 1)
    check([x["text"] for x in follows(mem)] == ["你事情0", "你事情1"], "一次最多收 2 条", follows(mem))
    mem = fresh(fold_answer([{"text": "你事情0", "due": D(2).isoformat()}]))
    seed(mem, [(NOW, "bange", "user", "随便说说")])
    put_follows(mem, [F("x", D(2), text="你事情0")])
    mem.consolidate(UID, 1)
    check(len(follows(mem)) == 1, "已有的不重复收", follows(mem))
    mem = fresh(fold_answer([{"text": "你新的事", "due": D(20).isoformat()}]))
    seed(mem, [(NOW, "bange", "user", "随便说说")])
    put_follows(mem, [F("o%d" % i, D(2 + i)) for i in range(10)] + [F("old", D(-5), state="asked")])
    mem.consolidate(UID, 1)
    fs = follows(mem)
    check(len(fs) == 10 and "o0" not in {x["id"] for x in fs} and "old" not in {x["id"] for x in fs},
          "open 最多 10 条，丢 due 最早的；过了能问的日子的一并清掉", [x["id"] for x in fs])

    print("== 他自己说了结果：close 掉，到期也不出红点（说计划、报结果分两次整理）")
    mem = fresh(fold_answer([{"text": "你明天下午有个面试", "due": D(1).isoformat()}]))
    seed(mem, [(at(-1), "bange", "user", "明天下午有个面试")])
    mem.consolidate(UID, 1)
    fid = follows(mem)[0]["id"]
    mem.llm = lambda m, a, b: fold_answer(close=[fid, "nosuchid"])
    seed(mem, [(NOW, "bange", "user", "面完了，拿到 offer 了")])
    mem.consolidate(UID, 1)
    check(follows(mem)[0]["state"] == "closed", "第二次整理给了 close：closed", follows(mem))
    check(memory._follow_pick(mem._load(UID), D(1)) is None, "到期那天也不出红点")
    mem = fresh(fold_answer(close=["a"]))
    seed(mem, [(NOW, "bange", "user", "随便说说")])
    put_follows(mem, [F("a", TODAY, state="asked")])
    mem.consolidate(UID, 1)
    check(follows(mem)[0]["state"] == "asked", "close 里不是 open 的不动（问过的还是 asked）")

    print("== 红点和领取：两边用同一个选法，每天两个自己合计一件")
    mem = fresh()
    put_follows(mem, [F("b1", D(-1), "bange"), F("p1", TODAY, "pingzuo")])
    check(mem.summary(UID)["due"] == {"id": "b1", "p": "bange"}, "两件都到期：due 最早的那件，在导师卡上", mem.summary(UID)["due"])
    check(mem.follow_take(UID, "pingzuo", "p1") is None, "点没有红点的伙伴卡、拿伙伴那件的 id：领不到")
    check(mem.follow_take(UID, "pingzuo", "b1") is None, "导师那件的 id 从伙伴这边：领不到")
    check(mem._load(UID)["follow_day"] is None and all(x["state"] == "open" for x in follows(mem)), "领不到的不占当天名额")
    check(mem.follow_take(UID, "bange", "zzz") is None, "不认识的 id：领不到")
    f = mem.follow_take(UID, "bange", "b1")
    check(f and f["id"] == "b1" and f["prev"] is None, "点导师卡、带着 b1：领到", f)
    check(mem.follow_take(UID, "bange", "b1") is None, "再领一次（第二个标签页）：领不到")
    check(mem.summary(UID)["due"] is None, "今天领过了：伙伴那件今天也不亮", mem.summary(UID)["due"])
    mem.follow_undo(UID, f)
    check(follows(mem)[0]["state"] == "open" and mem._load(UID)["follow_day"] is None
          and mem.summary(UID)["due"] == {"id": "b1", "p": "bange"}, "开场没生成出来（follow_undo）：放回去，红点还在")
    mem.follow_take(UID, "bange", "b1")
    mem.wipe(UID)
    mem.follow_undo(UID, f)
    check(not os.path.exists(mem._dir(UID)), "清空之后 follow_undo 不会把档案写回来")
    mem = fresh()
    put_follows(mem, [F("f", D(3))])
    check(mem.summary(UID)["due"] is None, "没到日子：没有红点")
    put_follows(mem, [F("f", TODAY)], day=TODAY.isoformat())
    check(mem.summary(UID)["due"] is None, "今天已经问过一件：没有红点")
    mem = fresh(follow=False)
    put_follows(mem, [F("f", TODAY)])
    check(mem.summary(UID)["due"] is None and mem.follow_take(UID, "bange", "f") is None, "总开关关掉：没有红点，也领不到")
    seed(mem, [(NOW, "bange", "user", "明天面试")])
    mem.llm = lambda m, a, b: (mem.calls.append(m), fold_answer([{"text": "你明天面试", "due": D(2).isoformat()}]))[1]
    mem.consolidate(UID, 1)
    check("follow" not in mem.calls[-1][0]["content"] and "【等着问的事】" not in mem.calls[-1][1]["content"]
          and follows(mem) == [F("f", TODAY)], "总开关关掉：提示词里没有，也不记")

    print("== 他报了结果、整理还没回来：红点不亮，也领不到（审核 2026-11-27 P1）")
    gate, asked = threading.Event(), threading.Event()
    mem = fresh(lambda m: (asked.set(), gate.wait(10), fold_answer(close=["b1"]))[2])
    put_follows(mem, [F("b1", TODAY, "bange", text="你周五下午有个面试")])
    check(mem.summary(UID)["due"] == {"id": "b1", "p": "bange"}, "报结果之前：导师卡有红点")
    seed(mem, [(NOW, "bange", "user", "面完了，拿到 offer 了"), (NOW, "bange", "ai", "嗯。")])
    check(mem.summary(UID)["due"] is None and mem.follow_take(UID, "bange", "b1") is None,
          "他的话落盘、还没整理：没有红点，点了也领不到")
    th = threading.Thread(target=mem.consolidate, args=(UID, 1)); th.start(); asked.wait(5)
    check(mem.summary(UID)["due"] is None and mem.follow_take(UID, "bange", "b1") is None,
          "整理正在等模型：还是没有红点、领不到")
    gate.set(); th.join()
    check(follows(mem)[0]["state"] == "closed" and mem.summary(UID)["due"] is None, "整理回来 close 了：closed，不再亮")
    mem = fresh(fold_answer())
    put_follows(mem, [F("b1", TODAY)])
    seed(mem, [(NOW, "bange", "user", "今天天气不错")])
    mem.consolidate(UID, 1)
    check(mem.summary(UID)["due"] == {"id": "b1", "p": "bange"}, "说的是别的事：整理完红点照常亮")

    print("== 他报了结果、回复还在生成（话还没落盘）就回首页：红点不亮、领不到（审核 2026-11-27 第四轮 P1）")
    gate, inside = threading.Event(), threading.Event()
    mem = fresh()
    app = app_with(mem, lambda m: (inside.set(), gate.wait(10), "嗯。")[2])
    put_follows(mem, [F("b1", TODAY, "bange", text="你周五下午有个面试")])
    th = threading.Thread(target=app.chat, args=("bange", {"u": UID, "nick": "测试",
                                                            "history": [{"role": "user", "content": "面试过了，拿到 offer 了"}]}, {}))
    th.start(); inside.wait(5)
    check(mem._load(UID)["msgs"] == 0 and mem.summary(UID)["due"] is None and mem.follow_take(UID, "bange", "b1") is None,
          "回复生成中（他的话还没落盘）：没有红点，点了也领不到")
    gate.set(); th.join()
    check(mem._load(UID)["msgs"] == 1 and mem.summary(UID)["due"] is None, "回复落盘了、还没整理：仍不亮")
    mem.llm = lambda m, a, b: fold_answer(close=["b1"])
    mem.consolidate(UID, 1)
    check(follows(mem)[0]["state"] == "closed" and mem.summary(UID)["due"] is None, "整理完 close 了：不再亮")
    t0 = time.time()
    while app.rec_jobs and time.time() - t0 < 10:
        time.sleep(0.05)

    print("== 回首页时后台正在整理：/api/sync 等它做完，不多调模型，之后就取得到红点（审核 2026-11-27 第四轮 P2）")
    gate, inside = threading.Event(), threading.Event()
    mem = fresh(lambda m: (inside.set(), gate.wait(10), fold_answer())[2])
    app = srv.App.__new__(srv.App); app.mem = mem  # App.fold is all /api/sync runs
    put_follows(mem, [F("b1", TODAY)])
    seed(mem, [(NOW, "bange", "user", "随便聊聊 %d" % i) for i in range(8)])
    bg = threading.Thread(target=mem.consolidate, args=(UID, 8)); bg.start(); inside.wait(5)
    check(mem.consolidate(UID, 1) is None and len(mem.calls) == 1, "后台整理中：聊天触发的那种不等，直接返回")
    out = {}
    sy = threading.Thread(target=lambda: out.update(r=app.fold(UID, 1, wait=10))); sy.start(); time.sleep(0.3)
    check(sy.is_alive() and mem.summary(UID)["due"] is None, "/api/sync 的整理在等；这时还没有红点（话没整理完）")
    gate.set(); bg.join(); sy.join()
    check(len(mem.calls) == 1 and out.get("r") is None and mem.summary(UID)["due"] == {"id": "b1", "p": "bange"},
          "后台那次做完，/api/sync 才返回：剩下的没有了、不再调模型；随后取得到红点", (len(mem.calls), out))

    print("== 午夜前领了 A、次日领了 B，A 才失败：放回 A，次日的名额不还（审核 2026-11-27 P2）")
    mem = fresh()
    put_follows(mem, [F("a", D(-1)), F("b", TODAY)])
    fa = mem.follow_take(UID, "bange", "a", today=D(-1))
    fb = mem.follow_take(UID, "bange", "b", today=TODAY)
    mem.follow_undo(UID, fa)
    prof = mem._load(UID)
    check(fa and fb and prof["follow_day"] == TODAY.isoformat() and [x["state"] for x in prof["follows"]] == ["open", "asked"],
          "A 放回 open，follow_day 仍是今天（B 用的）", (prof["follow_day"], [x["state"] for x in prof["follows"]]))
    check(mem.summary(UID)["due"] is None and mem.follow_take(UID, "bange", "a") is None, "今天不能再领一次")
    check(mem.summary(UID, today=D(1))["due"] == {"id": "a", "p": "bange"}, "明天 A 还在能问的日子里：照常亮")

    print("== 回来接着聊：同一个自己昨天没整理的话，今天也看得到")
    mem = fresh()
    seed(mem, [(at(-1), "pingzuo", "user", "今天跟同事小王因为周报闹别扭了，烦"),
               (NOW - datetime.timedelta(minutes=10), "pingzuo", "user", "刚才这句在页面发来的历史里")])
    ctx = mem.context(UID, "接着昨天那个说吧", "pingzuo")
    check("小王" in ctx and "刚才这句" not in ctx, "伙伴的上下文：有昨天那句，没有 10 分钟前那句（页面已经发了）", ctx[-200:])
    check("小王" in mem.context(UID, "接着昨天那个说吧", "bange"), "导师照样看得到")

    print("== 开场（真的 App.opening，桩模型）")
    said = []

    def model(msgs):
        said.append(msgs[0]["content"])
        return "周五那个面试，去了吗？"
    mem = fresh()
    app = app_with(mem, model)
    put_follows(mem, [F("b1", TODAY, "bange", text="你周五下午有个面试")])
    r = app.opening("bange", {"u": UID, "nick": "测试", "follow": "b1"}, {})
    check(r["follow"] == "b1" and "你周五下午有个面试" in said[-1] and "首页上给他留了一句话" in said[-1],
          "带着红点的 id：用 OPENING_FOLLOW，返回 follow", r)
    check(follows(mem)[0]["state"] == "asked" and mem._load(UID)["follow_day"] == TODAY.isoformat(), "问过了：asked，今天用掉")
    put_follows(mem, [F("b1", TODAY, "bange", text="你周五下午有个面试")])
    r = app.opening("pingzuo", {"u": UID, "nick": "测试"}, {})
    check(r["follow"] is None and "首页上给他留了一句话" not in said[-1] and "你周五下午有个面试" not in said[-1]
          and follows(mem)[0]["state"] == "open",
          "普通进伙伴（没带 id）：普通开场，不领导师那件", r)
    r = app.opening("pingzuo", {"u": UID, "nick": "测试", "follow": "b1"}, {})
    check(r["follow"] is None and follows(mem)[0]["state"] == "open", "伙伴带着导师那件的 id：不领，普通开场")
    long44 = "周五那个面试，去了吗？感觉怎么样，想说说就说，不想说也没关系，我们聊点别的也行，都可以。"
    size = lambda x: len(re.sub(r"\s", "", x))
    app3 = app_with(fresh(), lambda m: long44)
    put_follows(app3.mem, [F("b1", TODAY)])
    r = app3.opening("bange", {"u": UID, "nick": "测试", "follow": "b1"}, {})
    check(size(long44) > 40 and size(r["text"]) <= 40 and r["text"].startswith("周五那个面试"), "留言开场截到 40 字以内（不算空白）",
          "%d 字：%s" % (size(r["text"]), r["text"]))
    r = app3.opening("bange", {"u": UID, "nick": "测试"}, {})
    check(size(r["text"]) == size(long44), "普通开场还是原来的 80 字上限", size(r["text"]))
    p_said = []
    app2 = app_with(fresh(), lambda m: (p_said.append(m[0]["content"]), "那个面试应该过去了。")[1])
    put_follows(app2.mem, [F("p1", TODAY, "pingzuo", text="你周五下午有个面试")])
    app2.opening("pingzuo", {"u": UID, "nick": "测试", "follow": "p1"}, {})
    check("不用问号" in p_said[-1] and "只问这一个问题" not in p_said[-1], "伙伴的留言开场：不提问、不用问号")

    def boom(msgs):
        raise srv.LLMError("down")
    app = app_with(fresh(), boom)
    put_follows(app.mem, [F("b1", TODAY)])
    try:
        app.opening("bange", {"u": UID, "nick": "测试", "follow": "b1"}, {})
        raised = False
    except srv.LLMError:
        raised = True
    check(raised and follows(app.mem)[0]["state"] == "open" and app.mem._load(UID)["follow_day"] is None,
          "模型出错：错误照常抛给页面，这件事放回去，今天不算用掉")


# ---------------------------------------------------------------- live
RELATIVE = re.compile(r"今天|明天|后天|大后天|下周|下个月|这周|本周")
HAS_KCAL = re.compile(r"\d{2,4}\s*(大卡|千卡|卡|kcal)")


def live():
    print("\n== 真模型 · 回来接着聊（伙伴昨天听他说了同事的事；两种：已整理 / 只说了一句没整理）")
    talk = [(at(-1, 21), "pingzuo", "user", "今天跟同事小王闹别扭了"), (at(-1, 21), "pingzuo", "ai", "嗯，闹别扭挺磨人的。"),
            (at(-1, 21), "pingzuo", "user", "他把我写的周报改得乱七八糟还不说一声"),
            (at(-1, 21), "pingzuo", "ai", "被人改了东西还不打招呼，换谁心里都不舒服。"),
            (at(-1, 21), "pingzuo", "user", "我也不知道明天怎么跟他说话")]
    one = [(at(-1, 21), "pingzuo", "user", "今天跟同事小王因为周报闹别扭了，烦死了")]
    for name, rows, fold in (("已整理", talk, True), ("一句没整理", one, False)):
        for pid in ("pingzuo", "bange"):
            for i in range(3):
                mem = fresh()
                app = app_with(mem)
                seed(mem, rows)
                if fold:
                    mem.consolidate(UID, 1)
                r = app.chat(pid, {"u": UID, "nick": "测试", "history": [{"role": "user", "content": "接着昨天那个说吧"}]}, {})["reply"]
                check(re.search(r"小王|周报|同事", r), "%s · %s · 第%d次：“接着昨天那个说吧”接回小王/周报" % (name, pid, i + 1),
                      r.replace("\n", " ")[:90])
                r = app.chat(pid, {"u": UID, "nick": "测试", "history": [{"role": "user", "content": "帮我算一下午饭热量：一碗牛肉面"}]}, {})["reply"]
                check(HAS_KCAL.search(r) and not re.search(r"小王|周报|同事", r),
                      "%s · %s · 第%d次：“帮我算午饭热量”直接算，不提小王" % (name, pid, i + 1), r.replace("\n", " ")[:90])
                t0 = time.time()
                while app.rec_jobs and time.time() - t0 < 60:
                    time.sleep(0.1)

    print("\n== 真模型 · 抽取（今天说的一句话，整理后看记没记、日期对不对）")
    wd = TODAY.weekday()
    nxt = TODAY.replace(day=1) + datetime.timedelta(days=32)
    should = [("明天下午有个面试，有点慌", D(2)), ("后天下午去医院拿体检结果，有点紧张", D(3)),
              ("下周一要跟老板谈加薪，不知道怎么开口", D(7 - wd) + datetime.timedelta(days=1)),
              ("下个月 3 号要交论文初稿，还差好多", nxt.replace(day=4)), ("后天搬家，东西还没收拾完", D(3))]
    shouldnt = [[("user", "最近工作好累，真不想干了")],
                [("user", "我在想要不要跟我爸好好聊聊"), ("ai", "你可以明天找个时间跟他谈谈。"), ("user", "嗯")],
                [("user", "我一直担心我妈的身体")], [("user", "昨天面试完了，过了")],
                [("user", "我真的不想活了")], [("user", "过阵子可能换工作")]]
    hit = 0
    for text, due in should:
        mem = fresh()
        app_with(mem)
        seed(mem, [(NOW, "bange", "user", text), (NOW, "bange", "ai", "嗯。")])
        mem.consolidate(UID, 1)
        fs = follows(mem)
        good = len(fs) == 1 and fs[0]["due"] == due.isoformat() and not RELATIVE.search(fs[0]["text"])
        hit += good
        print("  %s %s -> %s（应为 %s）" % ("✓" if good else "✗", text, [(x["text"], x["due"]) for x in fs], due))
    check(hit >= 4, "该记的 5 句至少 4 句记下、日期对、写的是几号周几（不写明天后天）", hit)
    wrong = []
    for rows in shouldnt:
        mem = fresh()
        app_with(mem)
        seed(mem, [(NOW, "bange", role, text) for role, text in rows])
        mem.consolidate(UID, 1)
        if follows(mem):
            wrong.append((rows[0][1], follows(mem)))
    check(not wrong, "不该记的 6 句（好累、AI 建议的、担心妈妈、已面完、想死、过阵子）一句都不记", wrong)

    print("\n== 真模型 · 报结果（说计划、报结果分两次整理）")
    for result in ("面完了，拿到 offer 了！", "面试改期了，还没定什么时候"):
        mem = fresh()
        app_with(mem)
        seed(mem, [(at(-1, 20), "bange", "user", "明天下午有个面试，有点慌"), (at(-1, 20), "bange", "ai", "嗯。")])
        mem.consolidate(UID, 1)
        n0 = [x["state"] for x in follows(mem)]
        seed(mem, [(NOW, "bange", "user", result), (NOW, "bange", "ai", "嗯。")])
        mem.consolidate(UID, 1)
        fs = follows(mem)
        check(n0 == ["open"] and fs and all(x["state"] == "closed" for x in fs), "先记下，再说“%s”：closed" % result,
              [(x["text"], x["due"], x["state"]) for x in fs])

    print("\n== 真模型 · 开场（点红点进来，两个自己各 5 次）")
    for pid in ("bange", "pingzuo"):
        for i in range(5):
            mem = fresh()
            app = app_with(mem)
            seed(mem, [(at(-2, 20), pid, "user", "明天下午有个面试，有点慌")])
            prof = mem._load(UID)  # folded already (the page folds on the way home): else no dot can be taken
            prof["folded"], prof["upto"] = prof["msgs"], 1
            mem._save(UID, prof)
            put_follows(mem, [F("f1", TODAY, pid, text="你%d月%d日（周%s）下午有个面试" % (D(-1).month, D(-1).day, memory.WEEKDAYS[D(-1).weekday()]))])
            r = app.opening(pid, {"u": UID, "nick": "测试", "follow": "f1"}, {})
            t = r["text"]
            bad = [w for w in ("上回", "上次你说", "一直记着", "我记得你说", "恭喜", "过去了就过去了", "别多想", "明天") if w in t]
            q = pid == "pingzuo" and re.search(r"[？?]", t)
            n = len(re.sub(r"\s", "", t))
            check(r["follow"] == "f1" and "面试" in t and n <= 40 and not bad and not q,
                  "%s 第%d次：提到面试、≤40 字、没有套话%s" % (pid, i + 1, "、没有问号" if pid == "pingzuo" else ""),
                  "%d 字：%s %s" % (n, t, bad or ""))


offline()
if "--live" in sys.argv:
    live()
print("\n%d/%d" % (ok, total))
sys.exit(0 if ok == total else 1)
