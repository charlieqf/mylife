"""专项记录 (饮食、睡眠、心情) checks: storage and timing offline, and with --live what the model pulls out of real ways
of saying things and when the selves look records up. Made-up lines only; nothing is sent anywhere without --live.
  python tools/record_eval.py          # offline: clear/delete during an extraction, two at once, old records, edge cases
  python tools/record_eval.py --live   # also the model: extraction and lookup decisions (each lookup case 3 times)
Exit 1 when a check fails."""
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from _boot import ROOT, server

srv = server()
from memory import Memory  # noqa: E402  (server() puts server/ on the path)

UID = "recordevaluser00000000"
TODAY = datetime.date.today()
DAY = lambda n: (TODAY + datetime.timedelta(days=n)).isoformat()
ok = total = 0


def check(cond, what, note=""):
    global ok, total
    total += 1
    ok += bool(cond)
    print("%s %s %s" % ("PASS" if cond else "FAIL", what, note if not cond or note else ""))


class Stub:
    """The bits of mylife_server.App the record jobs use, with a model you script."""
    note_records = srv.App.note_records
    queue_records = srv.App.queue_records
    _run_records = srv.App._run_records
    day_meals = srv.App.day_meals

    def __init__(self, mem, model):
        self.mem, self.model, self.logs = mem, model, []
        self.rec_jobs, self.rec_lock, self.fold_pool = {}, threading.Lock(), ThreadPoolExecutor(max_workers=2)

    def llm(self, msgs, max_tokens=300, temperature=0.8, timeout=25, tidy=str.strip):
        return self.model(msgs), "stub", 0

    def log(self, *a, **k):
        self.logs.append(k)


class HttpApp(Stub):
    """Enough of App for srv.Handler's /api/self, /api/records and /api/event."""
    note_said = srv.App.note_said
    allow_ip = lambda self, ip, kind: True

    def log(self, *a, **k):
        self.logs.append(k)
        return "tid"


def serve():
    """srv.Handler on a free local port (srv.APP set by the caller) -> (httpd, post, get); each returns (status, JSON)."""
    import http.server, urllib.request, urllib.error
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (httpd.server_port, path),
                                     json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")
    return httpd, lambda path, body: call(path, body)[0], call


def fresh():
    return Memory(tempfile.mkdtemp(prefix="rec-eval-"), None)


def failing_open(name):
    """open() for memory.py that can't read files called `name` (writing them still works)."""
    real = open

    def fake(path, mode="r", *a, **k):
        if os.path.basename(str(path)) == name and mode.startswith("r"):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, mode, *a, **k)
    return fake


def job(app, kinds, words, at=None, pics=()):
    """What App.chat does at the start and end of a turn: note the versions and time, then hand over the extraction."""
    at = at or datetime.datetime.now()
    vers = {k: app.mem.record_ver(UID, k) for k in srv.RECORD_KINDS}
    turn = list(pics) + [words]
    return lambda: app.note_records(UID, "bange", kinds, turn, "（回复）", "tid", {}, at, vers)


def offline():
    print("== 清除、删除时正在提取的不写回（审核 P1）")
    for how in ("clear", "delete", "wipe"):
        mem, gate = fresh(), threading.Event()
        app = Stub(mem, lambda m: gate.wait(5) and '{"mood": {"mood": "低落", "score": 2, "about": "加班"}}')
        put = mem.record_put(UID, "mood", {"t": "2000-01-01T00:00:00", "d": DAY(0), "mood": "烦", "score": 2}, mem.record_ver(UID, "mood"))
        t = threading.Thread(target=job(app, ["mood"], "今天又加班到十点，好烦")); t.start(); time.sleep(0.2)
        if how == "clear":
            mem.record_clear(UID, "mood")
        elif how == "delete":
            mem.record_delete(UID, "mood", put["id"])
        else:
            mem.wipe(UID)
        n0 = len(mem.records(UID, "mood"))
        gate.set(); t.join()
        check(n0 == 0 and mem.records(UID, "mood") == [], "%s 之后放行提取，心情还是 0 条" % how, mem.records(UID, "mood"))
    mem = fresh()
    app = Stub(mem, lambda m: '{"mood": {"mood": "开心", "score": 4}}')
    mem.record_clear(UID, "mood")
    job(app, ["mood"], "今天挺开心的")()
    check(len(mem.records(UID, "mood")) == 1, "清除之后新说的照常记")

    print("== 同一天两条心情一起提取，只留一条，按说话先后（审核 P1）")
    t1, t2 = datetime.datetime.now() - datetime.timedelta(minutes=5), datetime.datetime.now()
    mem = fresh()
    slow = threading.Event()

    def model(m):  # the earlier turn's call comes back last
        if "被老板骂" in m[1]["content"]:
            slow.wait(5)
            return '{"mood": {"mood": "难过", "score": 2, "about": "被骂"}}'
        return '{"mood": {"mood": "好一点", "score": 3}}'
    app = Stub(mem, model)
    a = threading.Thread(target=job(app, ["mood"], "下午被老板骂了", t1))
    b = threading.Thread(target=job(app, ["mood"], "跟朋友聊完好多了", t2))
    a.start(); time.sleep(0.1); b.start(); b.join(); slow.set(); a.join()
    m = mem.records(UID, "mood")
    check(len(m) == 1 and m[0]["mood"] == "好一点" and m[0].get("from") == "难过", "直接并发：一条，难过 → 好一点", m)
    mem = fresh()
    order, slow = [], threading.Event()

    def model2(m):
        order.append(m[1]["content"][:12])
        if "被老板骂" in m[1]["content"]:
            time.sleep(0.3)
            return '{"mood": {"mood": "难过", "score": 2}}'
        return '{"mood": {"mood": "好一点", "score": 3}}'
    app = Stub(mem, model2)
    app.queue_records(UID, job(app, ["mood"], "下午被老板骂了", t1))
    app.queue_records(UID, job(app, ["mood"], "跟朋友聊完好多了", t2))
    app.fold_pool.shutdown(wait=True)
    m = mem.records(UID, "mood")
    check(len(m) == 1 and m[0]["mood"] == "好一点" and m[0].get("from") == "难过" and not app.rec_jobs,
          "排队：同一个人的提取一个接一个", m)
    mem = fresh()
    app = Stub(mem, lambda m: '{"sleep": {"night": -1, "bed": "01:00", "wake": "07:00", "hours": 6}}')
    ts = [threading.Thread(target=job(app, ["sleep"], "昨晚一点睡七点醒")) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    check(len(mem.records(UID, "sleep")) == 1, "四条同一晚的睡眠一起写，只留一条")

    print("== 长期保存：早于两周的也查得到，不丢最早的（审核 P2）")
    mem = fresh()
    ver = mem.record_ver(UID, "diet")
    mem.record_put(UID, "diet", {"t": DAY(-30) + "T12:30:00", "d": DAY(-30), "meal": "午饭", "items": [{"name": "牛肉面"}],
                                 "kcal": [600, 700]}, ver)
    check(mem.record_kinds(UID) == ["diet"], "30 天前的饮食也算有记录", mem.record_kinds(UID))
    dflt = mem.record_block(UID, "diet")
    check("这段没有记录" in dflt and DAY(-30) in dflt, "默认两周：说明这段没有、记录最早从哪天起", dflt.splitlines()[-2:])
    old = datetime.date.fromisoformat(DAY(-30))
    month_end = (old.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    blk = mem.record_block(UID, "diet", old.replace(day=1), month_end)
    check("牛肉面" in blk, "指定那个月：取得到", blk.splitlines()[1:2])
    big = [{"id": "%04d" % i, "t": DAY(i // 3 - 1002) + "T08:00:00", "d": DAY(i // 3 - 1002), "meal": ("早饭", "午饭", "晚饭")[i % 3],
            "items": [{"name": "粥", "amount": "1碗", "kcal": [150, 200]}, {"name": "包子", "amount": "2个", "kcal": [300, 360]}],
            "kcal": [450, 560]} for i in range(3005)]  # three meals a day for 1000 days
    mem._rec_write(UID, "diet", big)
    mem.record_put(UID, "diet", {"t": DAY(0) + "T08:00:00", "d": DAY(0), "meal": "早饭", "items": [{"name": "包子"}]}, ver)
    d = mem.records(UID, "diet")
    check(len(d) == 3006 and d[0]["id"] == "0000", "第 3006 条也存下，最早的还在", len(d))
    check(len(mem.record_block(UID, "diet", TODAY - datetime.timedelta(days=60), TODAY)) < 3200, "一大段时间也不会把 prompt 撑爆")

    print("== 餐次没说就不按钟点猜；有图没看成要记下（审核 P2）")
    at = datetime.datetime.combine(TODAY, datetime.time(20, 30))
    rec = srv.record_entry("diet", {"meal": "不确定", "items": [{"name": "炒饭", "kcal": [600, 700]}], "kcal": [600, 700]},
                           "bange", "tid", ["吃了一盘炒饭"], at)
    check(rec and rec["meal"] is None, "“不确定”存成没说哪顿，不是晚饭", rec and rec.get("meal"))
    rec = srv.record_entry("diet", {"meal": "午饭", "items": [{"name": "拌面"}]}, "bange", "tid",
                           [srv.IMAGE_MARK + "拌面一碗", srv.IMAGE_LOST, "这些一共多少卡"], at)
    check(rec and rec["pics"] == 1 and rec["lost"] == 1 and "没看成" in srv.meal_line(rec), "两张图一张没看成：记下少了一张",
          rec and srv.meal_line(rec))
    req = {"history": [{"role": "user", "content": srv.IMAGE_MARK + "拌面一碗"}, {"role": "user", "content": srv.IMAGE_LOST},
                       {"role": "user", "content": "这些一共多少卡"}]}
    msgs = srv.build_messages("bange", req, None, "", None, {}, "办事")
    check(srv.IMAGE_LOST_NOTE in msgs[0]["content"] and srv.IMAGE_LOST in msgs[-1]["content"], "回复时知道少看了一张")
    page = open(os.path.join(ROOT, "prototype", "mylife-prototype.html"), encoding="utf-8").read()
    check("var PIC_LOST='%s';" % srv.IMAGE_LOST[len(srv.IMAGE_MARK):] in page, "页面发的“没看成”和服务器认的是同一句（PIC_LOST = IMAGE_LOST）")
    mem = fresh()
    app = Stub(mem, lambda m: '{"diet": [{"meal": "早饭", "items": [{"name": "鸡蛋", "amount": "2个"}]}, '
                              '{"meal": "午饭", "items": [{"name": "面", "amount": "1碗"}]}]}')
    job(app, ["diet"], "我早上两个鸡蛋，中午一碗面")()
    check([r["meal"] for r in mem.records(UID, "diet")] == ["早饭", "午饭"], "一句话两顿，记两条")

    print("== 什么时候提取、什么时候查（审核 P2）")
    for line, kind in (("我早上两个鸡蛋，中午一碗面", "diet"), ("昨晚两点才躺下，早上六点就起床了", "sleep"),
                       ("中午一份麻辣烫加一杯奶茶", "diet"), ("凌晨三点还没睡着，六点闹钟就响了", "sleep")):
        got = srv.record_kinds_said([line], "（回复）", False)
        check(kind in got, "提取入口：%s -> %s" % (line, kind), got)
    asked = []
    fake = lambda m, n=20: asked.append(m[1]["content"]) or "睡眠 %s~%s" % (DAY(-55)[:8] + "01", DAY(-25))
    got = srv.records_needed(["我这周睡得怎么样", "那上个月呢"], ["sleep", "diet"], fake)
    check(asked and list(got) == ["sleep"] and got["sleep"] and got["sleep"][0].day == 1, "追问“那上个月呢”也去判断，并带上日期范围", got)
    got = srv.records_needed(["我昨晚睡了几个小时"], ["sleep"], lambda m, n=20: "睡眠 %s~%s" % (DAY(0), DAY(0)))
    check(got.get("sleep") and got["sleep"][0].isoformat() == DAY(-1), "问“昨晚睡了几个小时”、模型给的是今天：也翻昨天那晚（一晚记在入睡那天）", got)
    mem = fresh()
    mem.record_put(UID, "sleep", {"t": datetime.datetime.now().isoformat(timespec="seconds"), "d": DAY(-1), "bed": "02:00", "wake": "07:00",
                                  "hours": 5}, mem.record_ver(UID, "sleep"))
    check("5 小时" in mem.record_block(UID, "sleep", *got["sleep"]), "那一晚在给回复看的记录里", mem.record_block(UID, "sleep", *got["sleep"])[-120:])
    got = srv.records_needed(["他又吃醋了", "我今天下午去公园散步了一大圈，天气特别好"], ["diet"], lambda m, n=20: "饮食")
    check(got == {}, "长句不借上一句的词", got)
    offline_round2()
    offline_round3()


def offline_round2():
    """The second review of 2026-11-25."""
    print("== 全部清空时正在写的回复不写回（审核2 P1）")
    mem = fresh()
    mem.append(UID, "bange", [("user", "早先说的")])
    gen = mem.gen(UID)  # App.chat takes it when the request comes in
    mem.wipe(UID)
    n = mem.append(UID, "bange", [("user", "清空前发的这句"), ("ai", "清空后才回来的回复")], gen)
    check(n == 0 and not os.path.exists(os.path.join(mem.root, UID)), "清空后回来的回复不写进他的目录")
    mem.append(UID, "bange", [("user", "清空以后新说的")], mem.gen(UID))
    check(mem._read_log(UID, 0)[0][-1]["text"] == "清空以后新说的", "清空以后新说的照常记")

    print("== 删一条旧餐，不连累正在提取的新餐（审核2 P1）")
    mem, gate = fresh(), threading.Event()
    old = mem.record_put(UID, "diet", {"t": DAY(-1) + "T12:00:00", "d": DAY(-1), "meal": "午饭", "items": [{"name": "盖饭"}]},
                         mem.record_ver(UID, "diet"))
    app = Stub(mem, lambda m: gate.wait(5) and '{"diet": [{"meal": "晚饭", "items": [{"name": "饺子", "amount": "20个"}]}]}')
    t = threading.Thread(target=job(app, ["diet"], "晚上吃了二十个饺子")); t.start(); time.sleep(0.2)
    mem.record_delete(UID, "diet", old["id"])
    gate.set(); t.join()
    d = mem.records(UID, "diet")
    check([r["items"][0]["name"] for r in d] == ["饺子"], "昨天那顿删掉了，新的一顿照样记下", [r["items"][0]["name"] for r in d])
    mem, gate = fresh(), threading.Event()
    meal = mem.record_put(UID, "diet", {"t": datetime.datetime.now().isoformat(timespec="seconds"), "d": DAY(0), "meal": "午饭",
                                        "items": [{"name": "拌面"}], "kcal": [400, 450]}, mem.record_ver(UID, "diet"))
    app = Stub(mem, lambda m: gate.wait(5) and '{"diet": [{"meal": "午饭", "same": true, "items": [{"name": "拌面"}], "kcal": [500, 550]}]}')
    t = threading.Thread(target=job(app, ["diet"], "面其实是二两半，再算一下")); t.start(); time.sleep(0.2)
    mem.record_delete(UID, "diet", meal["id"])
    gate.set(); t.join()
    check(mem.records(UID, "diet") == [], "删掉的那顿，正在算的追问不会把它写回来", mem.records(UID, "diet"))

    print("== 删不掉时不报成功（审核2 P2）")
    from unittest import mock
    mem = fresh()
    mem.record_put(UID, "mood", {"t": "2000-01-01T00:00:00", "d": DAY(0), "mood": "烦"}, mem.record_ver(UID, "mood"))
    with mock.patch("memory.os.remove", side_effect=PermissionError("locked")):
        got = mem.record_clear(UID, "mood")
    check(got is False and len(mem.records(UID, "mood")) == 1, "删文件被拒：按类清除返回失败，记录还在")
    with mock.patch("memory.shutil.rmtree", side_effect=PermissionError("locked")):
        got = mem.wipe(UID)
    check(got == ["mood"], "删目录被拒：全部清空返回还剩什么", got)
    check(mem.record_clear(UID, "mood") is True and mem.wipe(UID) == [] and mem.wipe(UID) == [], "删得掉、或者本来就没有：成功")
    # the HTTP side: a failed delete is a 500, which the page already shows as 没删成
    srv.APP = HttpApp(fresh(), lambda m: '{"mood": {"mood": "难过", "score": 2}}')
    httpd, post, _ = serve()
    srv.APP.mem.record_put(UID, "mood", {"t": "2000-01-01T00:00:00", "d": DAY(0), "mood": "烦"}, srv.APP.mem.record_ver(UID, "mood"))
    with mock.patch("memory.os.remove", side_effect=PermissionError("locked")):
        st = post("/api/records/clear", {"u": UID, "kind": "mood"})
    check(st == 500, "接口：按类清除删不掉时回 500", st)
    with mock.patch("memory.shutil.rmtree", side_effect=PermissionError("locked")):
        st = post("/api/self/wipe", {"u": UID})
    check(st == 500, "接口：全部清空删不掉时回 500", st)
    check(post("/api/records/clear", {"u": UID, "kind": "mood"}) == 200 and post("/api/self/wipe", {"u": UID}) == 200, "接口：删得掉回 200")

    print("== 固定回复的那句也进记录（审核2 P2）")
    st = post("/api/event", {"u": UID, "p": "bange", "kind": "local", "why": "fixed", "user": "我只想说说，今天真的很难过",
                             "reply": "好，刀先收起来，不拆了。你说，我听着。"})
    for _ in range(50):
        if srv.APP.mem.records(UID, "mood"):
            break
        time.sleep(0.1)
    m = srv.APP.mem.records(UID, "mood")
    check(st == 200 and len(m) == 1 and m[0]["mood"] == "难过", "导师“只想说说”走固定回复：当天心情照样记下", m)
    post("/api/event", {"u": UID, "p": "bange", "kind": "local", "why": "crisis", "user": "不想活了", "reply": "我在。"})
    time.sleep(0.5)
    check(len(srv.APP.mem.records(UID, "mood")) == 1, "危机那句不拿来记心情")
    httpd.shutdown()

    print("== 跨月查饮食，营养和缺图还在（审核2 P2）")
    mem = fresh()
    mem.record_put(UID, "diet", {"t": DAY(-30) + "T12:30:00", "d": DAY(-30), "meal": "午饭", "items": [{"name": "鸡蛋面"}],
                                 "kcal": [200, 250], "protein_g": 16, "pics": 1, "lost": 1}, mem.record_ver(UID, "diet"))
    mem.record_put(UID, "diet", {"t": DAY(-30) + "T19:00:00", "d": DAY(-30), "meal": "晚饭", "items": [{"name": "粥"}]},
                   mem.record_ver(UID, "diet"))
    blk = mem.record_block(UID, "diet", TODAY - datetime.timedelta(days=45), TODAY)
    line = next(x for x in blk.splitlines() if "鸡蛋面" in x)
    check("蛋白质约 16 克（只有 1 顿算了）" in line and "没看成" in line and "2 顿里只有 1 顿算了热量" in line,
          "一个半月的摘要：留着蛋白质、缺图，说明合计只覆盖算过的", line)

    print("== 凌晨说的睡眠，日期只换算一次（审核2 P2）")
    two_am = datetime.datetime.combine(TODAY, datetime.time(2, 0))
    for night, at, want, what in ((-1, two_am, DAY(-1), "凌晨 2 点说“昨晚 23 点睡、刚醒”"),
                                  (0, two_am, DAY(-1), "凌晨 2 点说“还没睡”"),
                                  (-2, two_am, DAY(-2), "凌晨 2 点说“前晚”"),
                                  (-1, datetime.datetime.combine(TODAY, datetime.time(9, 0)), DAY(-1), "早上 9 点说“昨晚”"),
                                  (0, datetime.datetime.combine(TODAY, datetime.time(23, 0)), DAY(0), "晚上 11 点说“今晚”")):
        rec = srv.record_entry("sleep", {"night": night, "bed": "23:00"}, "bange", "tid", ["（他说的）"], at)
        check(rec and rec["d"] == want, "%s -> %s" % (what, want), rec and rec["d"])


def offline_round3():
    """The review of 2026-11-26."""
    from unittest import mock
    from memory import Unreadable
    now = datetime.datetime.now()
    said = lambda minutes: (now - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")

    print("== 删掉的那顿，排队里的更正不会换个 id 写回（审核3 P1）")
    for words, answer, want, what in (
            ("刚才那碗拌面其实是二两半，重算一下", '{"diet": [{"meal": "午饭", "same": true, "items": [{"name": "拌面", "amount": "二两半"}], '
             '"kcal": [500, 550]}]}', [], "更正的那顿在提取开始前删了：不写回"),
            ("晚上又吃了二十个饺子", '{"diet": [{"meal": "晚饭", "same": false, "items": [{"name": "饺子", "amount": "20个"}]}]}',
             ["饺子"], "同样删了上一顿，另说的新的一顿照常记")):
        mem, asked = fresh(), []
        meal = mem.record_put(UID, "diet", {"t": said(20), "d": DAY(0), "meal": "午饭", "items": [{"name": "葱油拌面"}], "kcal": [400, 450]},
                              mem.record_ver(UID, "diet"))
        app = Stub(mem, lambda m, a=answer: asked.append(m[0]["content"]) or a)
        run = job(app, ["diet"], words, now)  # the request comes in: versions taken
        mem.record_delete(UID, "diet", meal["id"])  # deleted while the reply is written, before the extraction runs
        run()
        names = [r["items"][0]["name"] for r in mem.records(UID, "diet")]
        check(names == want and asked and "删掉了" in asked[0] and "葱油" not in asked[0], what + "（提示里只说那顿删了，不给内容）",
              (names, asked and asked[0][-120:]))
    mem = fresh()
    mem.record_put(UID, "diet", {"t": said(-1), "d": DAY(0), "items": [{"name": "后来说的"}]}, mem.record_ver(UID, "diet"))
    check(mem.record_recent(UID, "diet", 3, now - datetime.timedelta(minutes=2), mem.record_ver(UID, "diet")) == [],
          "后说的那一顿先提取完：不当成这句之前的上一顿")

    print("== 读不出来的文件不当成空的，也不覆盖（审核3 P1）")
    mem = fresh()
    for name in ("盖饭", "拌面"):
        mem.record_put(UID, "diet", {"t": said(30), "d": DAY(0), "meal": "午饭", "items": [{"name": name}]}, mem.record_ver(UID, "diet"))
    path = mem._rec_path(UID, "diet")
    before = open(path, encoding="utf-8").read()
    app = Stub(mem, lambda m: '{"diet": [{"meal": "晚饭", "items": [{"name": "饺子"}]}], "mood": {"mood": "开心", "score": 4}}')
    with mock.patch("memory.open", failing_open("diet.json"), create=True):
        job(app, ["diet", "mood"], "晚上吃了二十个饺子，挺开心")()
        blk, kinds = mem.record_block(UID, "diet"), mem.record_kinds(UID)
        try:
            mem.records(UID, "diet")
            raised = False
        except Unreadable:
            raised = True
    check(open(path, encoding="utf-8").read() == before, "饮食文件读的时候出错：新的一顿不写，原来两顿原样还在")
    check(len(mem.records(UID, "mood")) == 1, "同一轮的心情照常记")
    check(any(l.get("why") == "unreadable" for l in app.logs), "日志里写明这顿为什么没记")
    check(raised and "读不出来" in blk and "diet" in kinds, "查记录时说读不出来，不说没记过", blk)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"items": [{"id": "a1", "d": "%s", "items": [{"name": "盖饭"}]}, {"id": ' % DAY(0))  # cut off mid-write
    broken = open(path, encoding="utf-8").read()
    for what, do in (("新的一顿", lambda: mem.record_put(UID, "diet", {"t": said(0), "d": DAY(0), "items": [{"name": "粥"}]},
                                                       mem.record_ver(UID, "diet"))),
                     ("删一条", lambda: mem.record_delete(UID, "diet", "a1"))):
        try:
            do()
            raised = False
        except Unreadable:
            raised = True
        check(raised and open(path, encoding="utf-8").read() == broken, "JSON 坏了：%s不写，文件原样留着" % what)
    mem.append(UID, "bange", [("user", "第一句")])
    sp = os.path.join(mem.root, UID, "self.json")
    with open(sp, "w", encoding="utf-8") as f:
        f.write('{"since": "2026-01-01", "msgs": 40, "portrait": "你')
    try:
        mem.append(UID, "bange", [("user", "第二句")])
        raised = False
    except Unreadable:
        raised = True
    check(raised and open(sp, encoding="utf-8").read().endswith('"你'), "记忆文件读不出来：不拿空的盖上去")
    check("认识第" in mem.context(UID, "第二句", "bange"), "只读来写提示时，读不出来就当不认识，聊天照常")

    print("== 部分清空：照实说还剩什么，重试能删干净（审核3 P2）")
    real_unlink = os.unlink

    def stuck(p, *a, **k):
        if os.path.basename(str(p)) == "sleep.json":
            raise PermissionError(13, "Permission denied", str(p))
        return real_unlink(p, *a, **k)
    srv.APP = HttpApp(fresh(), lambda m: "{}")
    mem = srv.APP.mem
    httpd, post, call = serve()

    def fill():
        mem.append(UID, "bange", [("user", "说过的话")])
        for kind, rec in (("diet", {"items": [{"name": "粥"}]}), ("sleep", {"bed": "23:00"}), ("mood", {"mood": "烦"})):
            mem.record_put(UID, kind, dict(rec, t=said(5), d=DAY(0)), mem.record_ver(UID, kind))
    fill()
    with mock.patch("os.unlink", stuck):
        left = mem.wipe(UID)
    check(left == ["sleep"] and not mem.records(UID, "diet") and len(mem.records(UID, "sleep")) == 1,
          "睡眠文件删不掉：其余的照样删掉，返回还剩睡眠", left)
    check(mem.wipe(UID) == [] and not os.path.exists(os.path.join(mem.root, UID)), "再清一次：删干净")
    fill()
    with mock.patch("os.unlink", stuck):
        st, body = call("/api/self/wipe", {"u": UID})
    check(st == 500 and body.get("left") == ["sleep"], "接口：回 500，并说还剩睡眠记录", (st, body))
    with mock.patch("memory.open", failing_open("sleep.json"), create=True):
        st, body = call("/api/records?u=%s&kind=sleep" % UID)
        st2, _ = call("/api/self?u=%s" % UID)
    check(st == 500, "接口：记录读不出来时回 500（页面说暂时取不到，不说没有记录）", st)
    check(st2 == 200, "接口：记忆读得出来照常回 200", st2)
    with mock.patch("memory.open", failing_open("self.json"), create=True):
        st, _ = call("/api/self?u=%s" % UID)
        st3, _ = call("/api/self/forget", {"u": UID, "id": "x"})
    check(st == 500 and st3 == 500, "接口：记忆读不出来时，查看和删除都回 500", (st, st3))
    httpd.shutdown()
    page = open(os.path.join(ROOT, "prototype", "mylife-prototype.html"), encoding="utf-8").read()
    check("toast('没连上服务器，什么都还没删" not in page and "leftSay(left)" in page, "页面不再说“什么都还没删”，照实说还剩什么")
    offline_round4()
    offline_round5()
    offline_round6()
    offline_round7()
    offline_round8()


def offline_round4():
    """The second review of 2026-11-26."""
    now = datetime.datetime.now()
    said = lambda minutes: (now - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")

    print("== 全部清空后，排队的提取不再把他的东西交给模型（审核4 P1）")
    mem, asked = fresh(), []
    a = mem.record_put(UID, "diet", {"t": said(30), "d": DAY(0), "meal": "午饭", "items": [{"name": "拌面", "amount": "二两"}]},
                       mem.record_ver(UID, "diet"))
    app = Stub(mem, lambda m: asked.append(m) or '{"diet": [{"meal": "午饭", "same": 1, "items": [{"name": "拌面"}]}]}')
    run = job(app, ["diet", "mood"], "刚才那碗拌面其实是二两半，今天有点累", now)
    mem.record_delete(UID, "diet", a["id"])
    mem.wipe(UID)
    run()
    check(asked == [] and not mem.rec_gone, "单删一顿再全部清空：排队的更正不调模型，删掉的那顿也不留", (len(asked), mem.rec_gone))
    check(all(mem.records(UID, k) == [] for k in srv.RECORD_KINDS), "什么都没写回")
    mem, asked = fresh(), []
    mem.record_put(UID, "diet", {"t": said(30), "d": DAY(0), "meal": "午饭", "items": [{"name": "葱油拌面", "amount": "二两"}]},
                   mem.record_ver(UID, "diet"))
    app = Stub(mem, lambda m: asked.append(m[0]["content"]) or '{"mood": {"mood": "累", "score": 2}}')
    run = job(app, ["diet", "mood"], "晚上吃了饺子，今天有点累", now)
    mem.record_clear(UID, "diet")
    run()
    check(asked and '"mood"' in asked[0] and '"diet"' not in asked[0] and "葱油" not in asked[0] and len(mem.records(UID, "mood")) == 1,
          "只清了饮食：只问心情，饮食的旧记录不进提示", asked and asked[0][:80])

    print("== 一句话记两餐，更正午饭不会盖掉早饭（审核4 P2）")
    for answer, want, what in (
            ('{"diet": [{"meal": "午饭", "same": true, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "午饭", "只说 same: true，按他说的午饭找"),
            ('{"diet": [{"meal": "午饭", "same": 1, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "午饭", "编号指错了，按他说的午饭找"),
            ('{"diet": [{"meal": "不确定", "same": 2, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "午饭", "没说哪顿，按编号"),
            ('{"diet": [{"meal": "早饭", "same": 1, "items": [{"name": "鸡蛋", "amount": "1个"}]}]}', "早饭", "更正早饭")):
        mem, asked = fresh(), []
        two = Stub(mem, lambda m: '{"diet": [{"meal": "早饭", "items": [{"name": "鸡蛋", "amount": "2个"}]}, '
                                  '{"meal": "午饭", "items": [{"name": "拌面", "amount": "1碗"}]}]}')
        job(two, ["diet"], "我早上两个鸡蛋，中午一碗拌面", now - datetime.timedelta(minutes=10))()
        fix = Stub(mem, lambda m, a=answer: asked.append(m[0]["content"]) or a)
        job(fix, ["diet"], "其实只有半碗" if want == "午饭" else "早上鸡蛋其实是一个", now)()
        d = {r["meal"]: r["items"][0]["amount"] for r in mem.records(UID, "diet")}
        changed = {"午饭": "半碗", "早饭": "1个"}[want]
        check(len(mem.records(UID, "diet")) == 2 and d.get(want) == changed and d.get("早饭" if want == "午饭" else "午饭"),
              "%s：改的是%s，另一顿还在" % (what, want), d)
    check(asked and "1. 今天 早饭" in asked[0] and "2. 今天 午饭" in asked[0], "提示里两顿按说的先后编号", asked and asked[0][-160:])


def chat_app(model=None):
    """A real mylife_server.App (srv.App.chat is what /api/chat runs) on a fresh memory; model(msgs, max_tokens) -> text
    instead of the real one when given. Nothing goes to the archive."""
    app = srv.App()
    app.mem, app.log = fresh(), lambda *a, **k: "tid"
    if model:
        app.llm = lambda msgs, max_tokens=300, temperature=0.8, timeout=25, tidy=str.strip: (model(msgs, max_tokens), "stub", 0)
    return app


def chat_turn(app, pid, said, hist=()):
    """He says `said` after `hist` (his and the self's lines) -> (reply, the history now); waits for his record jobs."""
    hist = list(hist) + [{"role": "user", "content": said}]
    reply = app.chat(pid, {"u": UID, "nick": "测试", "history": hist}, {})["reply"]
    t0 = time.time()
    while app.rec_jobs and time.time() - t0 < 90:
        time.sleep(0.05)
    return reply, hist + [{"role": "assistant", "content": reply}]


def offline_round5():
    """The third review of 2026-11-26."""
    now = datetime.datetime.now()
    two = '{"diet": [{"meal": "早饭", "items": [{"name": "鸡蛋", "amount": "2个"}]}, {"meal": "午饭", "items": [{"name": "拌面", "amount": "1碗"}]}]}'

    print("== 指不准是哪一顿，就不去改别的那顿（审核5 P2）")
    for answer, what, want in (
            ('{"diet": [{"meal": null, "same": 3, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "编号越界（只有两顿，给了 3），按菜名对上午饭",
             {"早饭": ["2个"], "午饭": ["半碗"]}),
            ('{"diet": [{"meal": "加餐", "same": "false", "items": [{"name": "苹果", "amount": "1个"}]}]}', "same 是字符串 \"false\"：另一顿，照常新增",
             {"早饭": ["2个"], "午饭": ["1碗"], "加餐": ["1个"]}),
            ('{"diet": [{"meal": "午饭", "same": 2, "items": [{"name": "拌面", "amount": "半碗"}]}, '
             '{"meal": "午饭", "same": 2, "items": [{"name": "卤蛋", "amount": "1个"}]}]}', "同一个编号这一轮第二次出现",
             {"早饭": ["2个"], "午饭": ["半碗", "1个"]}),
            ('{"diet": [{"meal": "午饭", "same": 3, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "编号越界但说了午饭，按午饭改",
             {"早饭": ["2个"], "午饭": ["半碗"]})):
        mem = fresh()
        job(Stub(mem, lambda m: two), ["diet"], "我早上两个鸡蛋，中午一碗拌面", now - datetime.timedelta(minutes=10))()
        job(Stub(mem, lambda m, a=answer: a), ["diet"], "其实只有半碗", now)()
        got = {}
        for r in mem.records(UID, "diet"):
            got.setdefault(r.get("meal"), []).append(r["items"][0]["amount"])
        check({k: sorted(v) for k, v in got.items()} == {k: sorted(v) for k, v in want.items()}, "%s：早饭、午饭都不被别的盖掉" % what, got)
    recent = [{"id": "a", "meal": "早饭"}, {"id": "b", "meal": "午饭"}]
    pick = lambda s, meal=None, used=(): (srv.same_meal({"same": s}, {"meal": meal}, recent, set(used))[0] or {}).get("meal")
    check([pick(0, "午饭"), pick(None, "午饭"), pick("否"), pick(True), pick("true"), pick(True, "早饭"), pick(True, "晚饭"), pick("2")]
          == [None, None, None, None, None, "早饭", None, "午饭"],
          "0、null、认不出的不算同一顿；只给 true、两顿都可能就不猜；说了另一顿就不改；\"2\" 也认")

    print("== 只说“其实只有半碗”，聊天入口也去更正那一顿（审核5 P2）")
    def model(answer):
        asked = []
        def m(msgs, n):
            if msgs[0]["content"].startswith(srv.RECORD_TURN[:20]):
                asked.append(msgs[0]["content"])
                return answer
            return "好，按半碗记。" if n >= 200 else ""
        return asked, m
    fix = '{"diet": [{"meal": null, "same": 2, "items": [{"name": "拌面", "amount": "半碗"}]}]}'
    asked, m = model(two)
    app = chat_app(lambda msgs, n: m(msgs, n))
    _, hist = chat_turn(app, "bange", "我早上两个鸡蛋，中午一碗拌面")
    asked2, m2 = model(fix)
    app.llm = lambda msgs, max_tokens=300, temperature=0.8, timeout=25, tidy=str.strip: (m2(msgs, max_tokens), "stub", 0)
    chat_turn(app, "bange", "其实只有半碗", hist)
    d = {r.get("meal"): r["items"][0]["amount"] for r in app.mem.records(UID, "diet")}
    check(asked2 and '"diet"' in asked2[0] and "2. 今天 午饭" in asked2[0], "“其实只有半碗”（回复“好，按半碗记。”）也去提取饮食，带着前面两顿",
          asked2 and asked2[0][-120:])
    check(d == {"早饭": "2个", "午饭": "半碗"}, "改的是午饭，早饭还在", d)
    for what, setup, said, want in (
            ("上一轮的那顿还没记好（还在排队），也认得出是在更正", "hist", "其实只有半碗", True),
            ("在另一个自己那边刚说过一顿，这边说“其实只有半碗”也去更正", "other", "其实只有半碗", True),
            ("四个钟头前说的一顿，现在说“其实只有半碗”不算更正", "old", "其实只有半碗", False),
            ("刚说过一顿，接着说的不相干的事不去提取饮食", "other", "其实我还是挺喜欢现在这份工作的", False)):
        asked, m = model(fix)
        app = chat_app(lambda msgs, n, m=m: m(msgs, n))
        hist = ()
        if setup == "hist":
            hist = [{"role": "user", "content": "我早上两个鸡蛋，中午一碗拌面"}, {"role": "assistant", "content": "两样都记下了。"}]
        else:
            t = now - datetime.timedelta(hours=4 if setup == "old" else 0, minutes=20)
            app.mem.record_put(UID, "diet", {"t": t.isoformat(timespec="seconds"), "d": DAY(0), "meal": "午饭",
                                             "items": [{"name": "拌面", "amount": "1碗"}]}, app.mem.record_ver(UID, "diet"))
        chat_turn(app, "pingzuo" if setup != "hist" else "bange", said, hist)
        check(bool(asked and '"diet"' in asked[0]) == want, what, asked and asked[0][:60])

def offline_round6():
    """The fourth review of 2026-11-26: the meal he named comes first, and a correction that fits no meal isn't counted twice."""
    now = datetime.datetime.now()
    said = lambda minutes: (now - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
    meal = lambda minutes, name, dish, amount, kcal: {"t": said(minutes), "d": DAY(0), "meal": name, "kcal": [kcal, kcal],
                                                       "items": [{"name": dish, "amount": amount}]}
    total = lambda mem: (re.search(r"今天）：.*?一共约 (\d+) 大卡", mem.record_block(UID, "diet")) or [None, None])[1]

    def run(before, answer, words):
        mem = fresh()
        for m in before:
            mem.record_put(UID, "diet", m, mem.record_ver(UID, "diet"))
        app = Stub(mem, lambda m: answer)
        job(app, ["diet"], words, now)()
        return mem, app, {(r.get("meal"), r["items"][0]["name"]): (r["kcal"][0], bool(r.get("unmatched"))) for r in mem.records(UID, "diet")}

    print("== 回复只写“卡”也去提取饮食（回归里看到的）")
    for words, reply, want in (("这两张一共多少卡", "一碗拌面 450 到 550 卡，加起来大概 2100 到 2400 卡。", True),
                               ("我的银行卡丢了", "先打电话挂失。", False), ("3张卡都刷爆了", "先别慌。", False)):
        check(("diet" in srv.record_kinds_said([words], reply, False)) == want, "%s -> %s" % (words, "提取饮食" if want else "不提取"))

    print("== 说了是午饭，就改午饭，不改后来那条没写餐次的（审核6 P1）")
    lunch, milk = meal(40, "午饭", "拌面", "1碗", 400), meal(10, None, "牛奶", "1杯", 100)
    fix = '{"diet": [{"meal": "午饭", "same": true, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}'
    mem, _, got = run([lunch, milk], fix, "午饭其实只有半碗")
    check(got == {("午饭", "拌面"): (200, False), (None, "牛奶"): (100, False)} and total(mem) == "300",
          "午饭 400 改成 200，牛奶 100 还在，当天合计 300", (got, total(mem)))
    mem, _, got = run([lunch, milk], fix.replace("true", "2"), "午饭其实只有半碗")
    check(got == {("午饭", "拌面"): (200, False), (None, "牛奶"): (100, False)}, "模型编号指着牛奶，也按他说的午饭改", got)
    mem, _, got = run([milk], fix, "午饭其实只有半碗")
    check(got[(None, "牛奶")] == (100, False), "前面没有午饭、只有牛奶：牛奶不被改成拌面", got)

    print("== 是在更正但对不上是哪一顿：原来的不动，也不重复算热量（审核6 P2）")
    egg, noodles = meal(60, "早饭", "鸡蛋", "2个", 100), meal(30, "午饭", "拌面", "1碗", 400)
    for answer, what in (('{"diet": [{"meal": null, "same": 3, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "编号越界，按菜名对上拌面"),
                         ('{"diet": [{"meal": "午饭", "same": 3, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "编号越界，说了午饭")):
        mem, _, got = run([egg, noodles], answer, "午饭其实只有半碗，200 大卡左右")
        check(got == {("早饭", "鸡蛋"): (100, False), ("午饭", "拌面"): (200, False)} and total(mem) == "300", "%s：两条，合计 300" % what,
              (got, total(mem)))
    mem, app, got = run([egg, noodles], '{"diet": [{"meal": null, "same": 3, "kcal": 200, "items": [{"name": "米饭", "amount": "半碗"}]}]}',
                        "其实只有半碗，200 大卡左右")
    block = mem.record_block(UID, "diet")
    check(got == {("早饭", "鸡蛋"): (100, False), ("午饭", "拌面"): (400, False), (None, "米饭"): (200, True)} and total(mem) == "500"
          and "没对上是哪一顿" in block, "菜名也对不上：原来两顿不动，这条更正记下但标着没对上、不算进合计（500）", (got, total(mem)))
    check(any(l.get("why") == "unmatched" for l in app.logs), "存档里记着 why=unmatched")
    check([r["meal"] for r in mem.record_recent(UID, "diet", 3, now, mem.record_ver(UID, "diet"))] == ["早饭", "午饭"],
          "没对上的那条不再当作一顿给下一次更正选")
    mem, _, got = run([egg, noodles], '{"diet": [{"meal": "午饭", "same": 2, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}, '
                                      '{"meal": "午饭", "same": 2, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}', "午饭其实只有半碗")
    check(total(mem) == "300", "模型把同一顿更正写了两遍：第二遍不算进合计", (got, total(mem)))
    mem, _, got = run([egg, noodles], '{"diet": [{"meal": "晚饭", "same": true, "kcal": 300, "items": [{"name": "饺子", "amount": "十个"}]}]}',
                      "晚上吃了十个饺子")
    check(got.get(("晚饭", "饺子")) == (300, False) and total(mem) == "800", "说的是前面没有的晚饭，模型却标了 same：照常新增、算进合计",
          (got, total(mem)))

def offline_round7():
    """The fifth review of 2026-11-26: a dish he named finds the meal, the day he named is kept to, and a meal from
    earlier in the day (past 3 hours) can still be corrected."""
    now = datetime.datetime.now()
    at = lambda **k: (now - datetime.timedelta(**k)).isoformat(timespec="seconds")
    meal = lambda t, d, name, dish, kcal: {"t": t, "d": d, "meal": name, "kcal": [kcal, kcal], "items": [{"name": dish, "amount": "1碗"}]}

    def run(before, answer, words):
        mem, asked = fresh(), []
        for m in before:
            mem.record_put(UID, "diet", m, mem.record_ver(UID, "diet"))
        job(Stub(mem, lambda m: asked.append(m[0]["content"]) or answer), ["diet"], words, now)()
        return mem, asked, sorted((r["d"], r.get("meal") or "", r["items"][0]["name"], r["kcal"][0], bool(r.get("unmatched")))
                                  for r in mem.records(UID, "diet"))

    print("== 说了菜名、没说哪顿：按菜名找，不改后来那杯牛奶（审核7 P1）")
    lunch, milk = meal(at(minutes=40), DAY(0), "午饭", "拌面", 400), meal(at(minutes=10), DAY(0), None, "牛奶", 100)
    for same in ("true", "2"):
        _, _, got = run([lunch, milk], '{"diet": [{"meal": null, "same": %s, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}' % same,
                        "刚才说的拌面其实只吃了半碗")
        check(got == [(DAY(0), "", "牛奶", 100, False), (DAY(0), "午饭", "拌面", 200, False)],
              "same=%s：拌面 400 改成 200，牛奶 100 不动（合计 300）" % same, got)

    print("== 说了是昨天那顿，就只改昨天的（审核7 P1）")
    yday, today = meal(at(minutes=20), DAY(-1), "午饭", "拌面", 400), meal(at(minutes=10), DAY(0), "午饭", "拌面", 500)
    for same in ("true", "2"):
        _, asked, got = run([yday, today], '{"diet": [{"meal": "午饭", "day": -1, "same": %s, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}'
                            % same, "昨天午饭只有半碗")
        check(got == [(DAY(-1), "午饭", "拌面", 200, False), (DAY(0), "午饭", "拌面", 500, False)],
              "same=%s：昨天 400 改成 200，今天 500 不动" % same, got)
    check(asked and "1. 昨天 午饭" in asked[0] and "2. 今天 午饭" in asked[0], "给模型的那几顿写着是哪天", asked and asked[0][-220:])
    _, _, got = run([yday, today], '{"diet": [{"meal": "午饭", "day": 0, "same": true, "kcal": 250, "items": [{"name": "拌面", "amount": "半碗"}]}]}',
                    "今天午饭其实只有半碗")
    check(got == [(DAY(-1), "午饭", "拌面", 400, False), (DAY(0), "午饭", "拌面", 250, False)], "说今天的：改今天的", got)

    print("== 三个钟头以前的那顿也能更正，不重复算（审核7 P2）")
    early = meal(at(hours=4), DAY(0), "午饭", "拌面", 400)
    mem, asked, got = run([early], '{"diet": [{"meal": "午饭", "same": 1, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}',
                          "中午那碗拌面其实只吃了一半，按 200 大卡算")
    check(got == [(DAY(0), "午饭", "拌面", 200, False)] and asked and "1. 今天 午饭" in asked[0], "四个钟头前的午饭在列表里，改成 200，只有一条", got)
    old = meal(at(days=2), DAY(-2), "午饭", "拌面", 400)
    mem, asked, got = run([old, early], '{"diet": [{"meal": "午饭", "day": -2, "same": true, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}',
                          "前天午饭其实只有半碗")
    check(got == [(DAY(-2), "午饭", "拌面", 200, False), (DAY(0), "午饭", "拌面", 400, False)], "前天的午饭（不在列表里）按那天去找，改的是它", got)
    check(asked and "上面没列的也算" in asked[0], "提示里说了：更正以前记过的、没列出来的也加 same")
    _, _, got = run([early], '{"diet": [{"meal": "午饭", "day": -3, "same": true, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}',
                    "大前天午饭只有半碗")
    check(got == [(DAY(-3), "午饭", "拌面", 200, False), (DAY(0), "午饭", "拌面", 400, False)], "那天没记过：就是那天的一顿，照常记", got)
    two = [meal(at(hours=5), DAY(0), "午饭", "拌面", 400), meal(at(hours=4), DAY(0), "午饭", "牛肉面", 500)]
    _, _, got = run(two, '{"diet": [{"meal": "午饭", "same": true, "kcal": 200, "items": [{"name": "米饭", "amount": "半碗"}]}]}', "午饭其实只有半碗米饭")
    check(sum(1 for g in got if g[4]) == 1 and sum(g[3] for g in got if not g[4]) == 900, "两顿午饭、菜名也对不上：两顿不动，这条标没对上、不算进合计", got)

def offline_round8():
    """The sixth review of 2026-11-26: "今天" said is a day too, the day isn't cut to a week, and a day's meals past the
    8 shown are still looked through."""
    now = datetime.datetime.now()
    when = lambda days, h=12: (datetime.datetime.combine(TODAY + datetime.timedelta(days=days), datetime.time(h, 30))
                               if days else now - datetime.timedelta(minutes=30)).isoformat(timespec="seconds")
    meal = lambda days, name, dish, kcal, h=12: {"t": when(days, h), "d": DAY(days), "meal": name, "kcal": [kcal, kcal],
                                                 "items": [{"name": dish, "amount": "1碗"}]}
    fix = lambda day, same="true": ('{"diet": [{"meal": "午饭", "day": %s, "same": %s, "kcal": 200, "items": [{"name": "拌面", "amount": "半碗"}]}]}'
                                    % (day, same))

    def run(before, answer, words):
        mem, asked = fresh(), []
        for m in before:
            mem.record_put(UID, "diet", m, mem.record_ver(UID, "diet"))
        job(Stub(mem, lambda m: asked.append(m[0]["content"]) or answer), ["diet"], words, now)()
        return mem, asked, sorted((r["d"], r.get("meal") or "", r["items"][0]["name"], r["kcal"][0], bool(r.get("unmatched")))
                                  for r in mem.records(UID, "diet"))

    print("== 说了“今天”，就不改昨天的（审核8 P1）")
    yday = meal(-1, "午饭", "拌面", 400)
    _, _, got = run([yday], fix(0), "说的是今天午饭，实际只吃了半碗拌面")
    check(got == [(DAY(-1), "午饭", "拌面", 400, False), (DAY(0), "午饭", "拌面", 200, False)],
          "只有昨天的午饭、他说的是今天的：昨天 400 不动，今天记一顿 200", got)
    _, _, got = run([yday], fix(0), "午饭其实只吃了半碗拌面")
    check(got == [(DAY(-1), "午饭", "拌面", 200, False)], "没说哪天（day 0 是模型默认的）：还是按午饭找到昨天那顿", got)

    print("== 九天前的就是九天前，不截成七天前（审核8 P1）")
    nine, seven = meal(-9, "午饭", "拌面", 400), meal(-7, "午饭", "拌面", 500)
    _, _, got = run([nine, seven], fix(-9), "九天前那顿午饭的拌面其实只吃了半碗")
    check(got == [(DAY(-9), "午饭", "拌面", 200, False), (DAY(-7), "午饭", "拌面", 500, False)], "改的是九天前的，七天前的不动", got)
    _, _, got = run([nine, seven], fix(-500), "很久以前那顿午饭其实只吃了半碗")
    check(got == [(DAY(-9), "午饭", "拌面", 400, False), (DAY(-7), "午饭", "拌面", 500, False)], "超出一年的：什么都不改、不写", got)
    _, _, got = run([], '{"diet": [{"meal": "晚饭", "day": -9, "kcal": 900, "items": [{"name": "火锅", "amount": "一顿"}]}]}', "九天前晚上吃了顿火锅")
    check(got == [(DAY(-9), "晚饭", "火锅", 900, False)], "新记的一顿也记在九天前那天", got)
    rec = srv.record_entry("sleep", {"night": -9, "bed": "23:00", "wake": "07:00"}, "bange", "tid", ["九天前那晚 11 点睡 7 点起"], now)
    check(rec and rec["d"] == DAY(-9), "睡眠的“哪一晚”也不截", rec and rec["d"])
    check(srv.record_entry("sleep", {"night": -500, "bed": "23:00"}, "bange", "tid", ["很久以前"], now) is None, "超出一年的那晚不记")

    print("== 昨天那顿排在给模型看的 8 条之外，也能找到（审核8 P2）")
    later = [meal(0, "加餐", "水果%d" % i, 50, h) for i, h in enumerate(range(7, 15))]  # eight said today, after it
    for m, t in zip(later, range(8)):
        m["t"] = (now - datetime.timedelta(minutes=80 - t * 5)).isoformat(timespec="seconds")
    yday["t"] = (now - datetime.timedelta(minutes=200)).isoformat(timespec="seconds")
    mem, asked, got = run([yday] + later, fix(-1), "昨天午饭其实只有半碗")
    lunch = [g for g in got if g[1] == "午饭"]
    check(asked and "昨天 午饭" not in asked[0] and asked[0].count("今天 加餐") == 8 and lunch == [(DAY(-1), "午饭", "拌面", 200, False)],
          "列表里没有它（只给 8 条），按昨天整天的记录找到，改成 200，没多出一顿", lunch)
    early = dict(meal(0, "午饭", "拌面", 400), t=(now - datetime.timedelta(minutes=200)).isoformat(timespec="seconds"))
    mem, asked, got = run([early] + later, fix(0), "今天午饭其实只有半碗")
    check([g for g in got if g[1] == "午饭"] == [(DAY(0), "午饭", "拌面", 200, False)], "今天的也一样：列表外的午饭照样找到", got)

def live():
    print("== 模型：真实说法提取（--live）")
    app = Stub(fresh(), lambda m: srv.complete(m, 900, 0, 30)[0])
    for words, kinds, want, what in (
            ("我早上两个鸡蛋，中午一碗面", ["diet"], lambda r: sorted(x["meal"] for x in r["diet"]) == ["午饭", "早饭"], "两顿各记一条"),
            ("昨晚两点才躺下，早上六点就起床了", ["sleep"],
             lambda r: len(r["sleep"]) == 1 and r["sleep"][0]["d"] == DAY(-1) and r["sleep"][0].get("bed", "").endswith("2:00")
             and r["sleep"][0].get("wake", "").endswith("6:00"), "记在昨晚，2 点睡 6 点起"),
            ("帮我算算一盘炒饭多少卡", ["diet"], lambda r: all(x["meal"] is None for x in r["diet"]), "没说哪顿就空着"),
            ("一碗米饭大概多少卡", ["diet"], lambda r: r["diet"] == [], "只问一般知识不记")):
        app.mem = fresh()
        job(app, kinds, words)()
        recs = {k: app.mem.records(UID, k) for k in kinds}
        check(want(recs), "%s：%s" % (what, words), json.dumps(recs, ensure_ascii=False)[:240])

    print("== 模型：一段连着的对话（--live）")
    app.mem = mem = fresh()
    meal = srv.IMAGE_MARK + "照片。拌面一碗约二两，清汤馄饨约十个，煎包四个，一瓶500毫升白酒。"
    talk = lambda kinds, words, reply, pics=(): app.note_records(UID, "bange", kinds, list(pics) + [words], reply, "tid", {},
                                                                datetime.datetime.now(), {k: mem.record_ver(UID, k) for k in kinds})
    talk(["diet"], "计算一下卡路里", "不算酒一共1000到1200大卡：\n拌面350到400\n馄饨200到250\n煎包300到360\n白酒没算，喝一两140大卡。", [meal])
    d = mem.records(UID, "diet")
    check(len(d) == 1 and d[0]["kcal"] == [1000, 1200] and d[0]["pics"] == 1, "算过的一顿记下", d and d[0].get("kcal"))
    talk(["diet"], "酒喝了二两，也算进去", "加280大卡，这顿一共1280到1480大卡。")
    d = mem.records(UID, "diet")
    check(len(d) == 1 and d[0]["kcal"][0] >= 1200 and d[0]["pics"] == 1, "追问同一顿是更新，不是新增", d and d[-1].get("kcal"))
    talk(["diet"], "晚上吃了一碗牛肉面，加了个蛋", "牛肉面加蛋，挺实在的一顿。")
    d = mem.records(UID, "diet")
    check(len(d) == 2 and d[-1]["meal"] == "晚饭" and not d[-1].get("kcal"), "随口说的另一顿也记（没算热量就空着）",
          d and [i["name"] for i in d[-1]["items"]])
    talk(["sleep", "mood"], "昨晚两点才睡，七点就醒了，现在困死了", "五个小时，难怪困。")
    talk(["sleep"], "对了昨晚半夜还醒了两次", "醒两次，那更没睡踏实。")
    s = mem.records(UID, "sleep")
    check(len(s) == 1 and s[0]["d"] == DAY(-1) and s[0].get("wakeups") == 2 and s[0].get("bed"), "同一晚补充的合进同一条",
          json.dumps(s, ensure_ascii=False)[:200])
    talk(["mood"], "下午被老板当着全组骂了，好难受", "当着全组，这一下够难堪的。")
    talk(["mood"], "刚跟朋友吃了顿饭聊了聊，好多了", "能说出来就松了一半。")
    m = mem.records(UID, "mood")
    check(len(m) == 1 and m[0].get("from") and (m[0].get("score") or 0) >= 3, "心情一天一条，变化记成 从 → 到",
          json.dumps(m, ensure_ascii=False)[:200])
    n0 = [len(mem.records(UID, k)) for k in srv.RECORD_KINDS]
    talk(["diet", "mood"], "帮我算一下房贷，100万30年利率3.5每月还多少", "每月大约还4490元。")
    check([len(mem.records(UID, k)) for k in srv.RECORD_KINDS] == n0, "不相干的（算房贷）什么都不记")
    for _ in range(2):  # review 2026-11-26: two meals said in one line, then the 午饭 corrected
        app.mem = mem = fresh()
        talk(["diet"], "我早上两个鸡蛋，中午一碗拌面", "两个鸡蛋约140大卡，一碗拌面约450到550大卡。")
        talk(["diet"], "午饭其实只有半碗", "半碗的话约230到280大卡。")
        d = {r.get("meal"): r for r in mem.records(UID, "diet")}
        check(len(mem.records(UID, "diet")) == 2 and "早饭" in d and "午饭" in d and "鸡蛋" in d["早饭"]["items"][0]["name"]
              and "半" in json.dumps(d["午饭"], ensure_ascii=False), "两顿之后更正午饭：改午饭，早饭还在",
              json.dumps([(r.get("meal"), r["items"]) for r in mem.records(UID, "diet")], ensure_ascii=False)[:200])

    print("== 模型：只是问起记过的那顿，那顿不改（--live，3 次）")
    for _ in range(3):  # review run 2026-11-26: the reply's breakdown came back as a "correction", 牛肉面 became 面条、牛肉、蛋
        app.mem = mem = fresh()
        mem.record_put(UID, "diet", {"t": (datetime.datetime.now() - datetime.timedelta(minutes=20)).isoformat(timespec="seconds"),
                                     "d": DAY(0), "meal": "午饭", "kcal": [600, 750],
                                     "items": [{"name": "牛肉面", "amount": "1碗", "kcal": [450, 600]}, {"name": "鸡蛋", "amount": "1个", "kcal": [70, 80]}]},
                       mem.record_ver(UID, "diet"))
        talk(["diet"], "我今天中午那顿多少热量来着",
             "中午那碗牛肉面加个蛋，记的是 600 到 750 大卡：面条 350 到 450，牛肉 100 到 150，蛋 70 到 80，汤和油 50 到 100。")
        d = mem.records(UID, "diet")
        check(len(d) == 1 and d[0]["items"][0]["name"] == "牛肉面" and d[0]["kcal"] == [600, 750], "问“中午那顿多少热量来着”：照旧是牛肉面 600-750",
              json.dumps([(r.get("meal"), [i["name"] for i in r["items"]], r.get("kcal")) for r in d], ensure_ascii=False))

    print("== 模型：从聊天入口，两顿之后只说“其实只有半碗”（--live）")
    for _ in range(2):  # review 2026-11-26: no meal and no food named, so the turn was never looked at
        app = chat_app()
        r1, hist = chat_turn(app, "bange", "我早上两个鸡蛋，中午一碗拌面")
        n1 = len(app.mem.records(UID, "diet"))
        r2, _ = chat_turn(app, "bange", "其实只有半碗", hist)
        d = {r.get("meal"): r for r in app.mem.records(UID, "diet")}
        check(n1 == 2 and len(d) == 2 and "鸡蛋" in d.get("早饭", {"items": [{"name": ""}]})["items"][0]["name"]
              and "半" in json.dumps(d.get("午饭"), ensure_ascii=False), "改的是午饭，早饭还在（回复：%s）" % clip_reply(r2),
              json.dumps([(r.get("meal"), r["items"]) for r in app.mem.records(UID, "diet")], ensure_ascii=False)[:200])

    def noodle_and_milk(app):
        """拌面 once and half, 牛奶 once: whether the model kept the 牛奶 as a meal of its own or added it to 午饭."""
        items = [i for r in app.mem.records(UID, "diet") if not r.get("unmatched") for i in r["items"]]
        noodles, milk = [i for i in items if "面" in i["name"]], [i for i in items if "奶" in i["name"]]
        return len(noodles) == 1 and "半" in str(noodles[0].get("amount")) and len(milk) == 1

    print("== 模型：午饭、一杯没说哪顿的牛奶，再更正午饭（--live）")
    for _ in range(3):  # review 2026-11-26: the correction went onto the 牛奶, said later without a meal; and the 牛奶 counted twice
        app = chat_app()
        _, hist = chat_turn(app, "bange", "中午吃了一碗拌面")
        _, hist = chat_turn(app, "bange", "刚又喝了一杯牛奶", hist)
        r3, _ = chat_turn(app, "bange", "午饭其实只有半碗", hist)
        check(noodle_and_milk(app), "拌面改成半碗，牛奶还在、只算一次（回复：%s）" % clip_reply(r3),
              json.dumps([(r.get("meal"), [(i["name"], i.get("amount")) for i in r["items"]], r.get("unmatched"))
                          for r in app.mem.records(UID, "diet")], ensure_ascii=False)[:240])

    print("== 模型：从聊天入口，说菜名不说餐次 / 说昨天的 / 四个钟头前的（--live）")
    show = lambda app: json.dumps([(r["d"][5:], r.get("meal"), [(i["name"], i.get("amount")) for i in r["items"]], r.get("unmatched"))
                                   for r in app.mem.records(UID, "diet")], ensure_ascii=False)[:260]
    for _ in range(3):  # review 2026-11-26: "刚才说的拌面其实只吃了半碗" went onto the 牛奶 said after it
        app = chat_app()
        _, hist = chat_turn(app, "bange", "中午吃了一碗拌面")
        _, hist = chat_turn(app, "bange", "刚又喝了一杯牛奶", hist)
        chat_turn(app, "bange", "刚才说的拌面其实只吃了半碗", hist)
        check(noodle_and_milk(app), "说了拌面没说哪顿：拌面改成半碗，牛奶还在、只算一次", show(app))
    for _ in range(2):  # review 2026-11-26: "昨天午饭只有半碗" went onto today's
        app = chat_app()
        _, hist = chat_turn(app, "bange", "昨天中午吃了一碗拌面，忘了跟你说")
        _, hist = chat_turn(app, "bange", "今天中午又吃了一碗拌面", hist)
        chat_turn(app, "bange", "昨天午饭其实只有半碗", hist)
        by = {r["d"]: r for r in app.mem.records(UID, "diet") if not r.get("unmatched")}
        check(len(by) == 2 and "半" in json.dumps(by.get(DAY(-1)), ensure_ascii=False) and "半" not in json.dumps(by.get(DAY(0)), ensure_ascii=False),
              "说了是昨天的：改昨天的，今天的不动", show(app))
    for _ in range(2):  # review 2026-11-26: a meal told 4 hours before was out of reach, the correction came in as a second one
        app = chat_app()
        app.mem.record_put(UID, "diet", {"t": (datetime.datetime.now() - datetime.timedelta(hours=4)).isoformat(timespec="seconds"),
                                         "d": DAY(0), "meal": "午饭", "kcal": [400, 450], "items": [{"name": "拌面", "amount": "1碗", "kcal": [400, 450]}]},
                           app.mem.record_ver(UID, "diet"))
        chat_turn(app, "bange", "中午那碗拌面其实只吃了一半")
        d = [r for r in app.mem.records(UID, "diet") if not r.get("unmatched")]
        check(len(d) == 1 and "半" in json.dumps(d[0], ensure_ascii=False), "四个钟头前的午饭：改它，不多出一顿", show(app))

    print("== 模型：从聊天入口，说“今天” / 九天前 / 昨天那顿在 8 条之外（--live）")
    noon = lambda days: datetime.datetime.combine(TODAY + datetime.timedelta(days=days), datetime.time(12, 30)).isoformat(timespec="seconds")
    lunch = lambda days, kcal: {"t": noon(days), "d": DAY(days), "meal": "午饭", "kcal": [kcal, kcal],
                                "items": [{"name": "拌面", "amount": "1碗", "kcal": [kcal, kcal]}]}

    def seeded(*meals):
        app = chat_app()
        for m in meals:
            app.mem.record_put(UID, "diet", m, app.mem.record_ver(UID, "diet"))
        return app
    by_day = lambda app: {d: [r for r in app.mem.records(UID, "diet") if r["d"] == d and r.get("meal") == "午饭" and not r.get("unmatched")]
                          for d in {r["d"] for r in app.mem.records(UID, "diet")}}
    for _ in range(2):  # review 2026-11-26: "说的是今天午饭" went onto yesterday's, the only one there was
        app = seeded(lunch(-1, 400))
        chat_turn(app, "bange", "说的是今天午饭，实际只吃了半碗拌面")
        y = by_day(app).get(DAY(-1), [])
        check(len(y) == 1 and y[0]["kcal"] == [400, 400] and y[0]["items"][0]["amount"] == "1碗", "说了今天：昨天那顿不动", show(app))
    for _ in range(2):  # review 2026-11-26: day -9 was cut to -7
        app = seeded(lunch(-9, 400), lunch(-7, 500))
        chat_turn(app, "bange", "九天前那顿午饭的拌面其实只吃了半碗")
        b = by_day(app)
        check(len(b.get(DAY(-9), [])) == 1 and "半" in json.dumps(b[DAY(-9)], ensure_ascii=False)
              and b.get(DAY(-7), [{}])[0].get("kcal") == [500, 500], "九天前：改九天前的，七天前的不动", show(app))
    for _ in range(2):  # review 2026-11-26: yesterday's was past the 8 shown and came in as a second one
        app = seeded(lunch(-1, 400), *[{"t": (datetime.datetime.now() - datetime.timedelta(minutes=90 - 5 * i)).isoformat(timespec="seconds"),
                                         "d": DAY(0), "meal": "加餐", "items": [{"name": "水果%d" % i, "amount": "1份"}]} for i in range(8)])
        chat_turn(app, "bange", "昨天午饭其实只有半碗")
        y = by_day(app).get(DAY(-1), [])
        check(len(y) == 1 and "半" in json.dumps(y[0], ensure_ascii=False), "昨天那顿在 8 条之外：改它，不多出一顿", show(app))

    print("== 模型：什么时候查记录（--live，每条 3 次）")
    ask = lambda m, n=20: srv.complete(m, n, 0, 8)[0]
    last_month = (TODAY.replace(day=1) - datetime.timedelta(days=1))
    have = ["diet", "sleep", "mood"]
    recent = lambda span: span is None or TODAY - datetime.timedelta(days=srv.RECORD_DAYS) < span[0] <= span[1] == TODAY
    for users, want in (
            (["我今天一共吃了多少热量"], {"diet": recent}), (["我这周睡得怎么样"], {"sleep": recent}),
            (["我最近心情是不是好点了"], {"mood": recent}), (["这几天白天特别困，没精神"], {"sleep": recent}),
            (["我昨晚睡了几个小时"], {"sleep": lambda span: span is None or span[0] <= TODAY - datetime.timedelta(days=1) <= span[1]}),
            (["他又吃醋了，烦死"], {}), (["累死了，睡一觉就好了"], {}), (["今天天气不错"], {}), (["被骂了好难受，不想说话"], {}),
            (["上个月我都吃了些什么"], {"diet": (last_month.replace(day=1), last_month)}),
            (["我这周睡得怎么样", "那上个月呢"], {"sleep": (last_month.replace(day=1), last_month)}),
            (["%d月%d号那天我吃了啥" % (TODAY.month, 3)], {"diet": (TODAY.replace(day=3), TODAY.replace(day=3))} if TODAY.day > 3 else None)):
        if want is None:
            continue
        got = [srv.records_needed(users, have, ask) for _ in range(3)]
        fits = lambda g: set(g) == set(want) and all(w(g[k]) if callable(w) else g[k] == w for k, w in want.items())
        check(all(fits(g) for g in got), "%s -> %s" % (" / ".join(users), {k: "最近" if callable(w) else w for k, w in want.items()}), got)


def clip_reply(text):
    return text.replace("\n", " ")[:30]


offline()
if "--live" in sys.argv:
    live()
print("\n%d/%d" % (ok, total))
sys.exit(0 if ok == total else 1)
