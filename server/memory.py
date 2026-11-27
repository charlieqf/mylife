"""Long-term memory: each user's "other selves" get to know them a little more after every talk.

Per anonymous user id (random, made in the browser, kept in localStorage; no account):
  /var/cache/mylife/users/<uid>/log.jsonl     every line said (user, persona replies, cards, closing lines)
  /var/cache/mylife/users/<uid>/self.json     what the two selves know about the user:
      since      first day
      msgs       how many things the user has said in total
      folded     how many of those are already folded into the profile (msgs - folded = pending)
      upto       how many log lines have been folded
      portrait   "你是怎样的人" (<=220 字, second person), rewritten as they learn more
      notes      facts worth remembering ("你……", <=30 字 each, newest first, max 80)
      episodes   one line per talk, max 300; d = the day of that talk's last line
      follows    things he said are coming up and will have an outcome (面试、拿体检结果……), picked up when folding:
                 [{id, text, due (the day after it happens), p (the self he told), state: open | asked | closed}].
                 An open one shows as a red dot on that self's card from due to due+2, at most one a day across both
                 selves (follow_day); tapping it opens with one line about it (follow_take). closed = he told the
                 outcome himself. See docs/方案_接着上次的事与红点留言.md.
  /var/cache/mylife/users/<uid>/diet.json     专项记录 · 饮食: every meal he had the selves work out (calories, nutrition)
      {"items": [{id, t, d, meal (null when he didn't say which), items: [{name, amount, kcal: [lo, hi] | null}], kcal,
                  protein_g, fat_g, carb_g, note, p, tid, pics, lost (pictures that couldn't be read), said}]}
  /var/cache/mylife/users/<uid>/sleep.json    专项记录 · 睡眠: one per night he told them about (a nap goes into that day's)
      {"items": [{id, t, d (the night's date), bed "23:30", wake "07:10", hours, quality, wakeups, nap, note, p, tid, said}]}
  /var/cache/mylife/users/<uid>/mood.json     专项记录 · 心情: one per day, updated as he talks
      {"items": [{id, t, d, mood, from (the day's first mood, when it changed), score 1-5, about, note, p, tid, said}]}
  Records: kept for good (no cap; a year of three meals a day is ~300 KB); t = when he said it, d = the day it is about.
  Both selves read them when the server decides the turn needs them (record_block: the last RECORD_DAYS days, or the
  stretch he asks about); he sees, deletes and clears them himself (/api/records).
Folding new lines in ("consolidate") is one LLM call that returns JSON. The server runs it in the
background after every 8 new user lines and after a closing line, and /api/sync runs it when the user
leaves a chat.
"""
import datetime
import json
import os
import re
import secrets
import shutil
import sys
import threading
from contextlib import contextmanager

UID_RE = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
NOTE_CAP, EPISODE_CAP = 80, 300
FOLLOW_CAP, FOLLOW_DAYS, FOLLOW_AHEAD = 10, 3, 30  # open follows kept; days one can be asked (due..due+2); due at most this far out
SESSION_GAP = datetime.timedelta(hours=2)  # the page sends only this visit (sessionMsgs: a 2-hour silence starts a new one)
LEVELS = (3, 8, 16, 28, 45)  # score thresholds for how "formed" the other selves look (0..5)
NEW_USER_LINE = "（你们刚认识，你还不知道他的任何事：不要提“上次”“上周”“那件事”这类往事。）"
# How much to say when he brings something heavy or asks to be talked through it; grows with how well the selves
# know him. (up to this level, hard cap in 字 that the server trims to, what the prompt asks for)
DEPTH = ((1, 900, "你们刚认识，你只知道他刚说的这些：不往外猜他的生活，不装作很懂他。但了解得少，不妨碍把他刚说的这件事说透：他带着情绪来、讲一件压着他的事、或要你开导时，就围着他的原话往深里说，先安慰，再分析，再化开，五六小段，六七百字左右，最多八百字。"),
         (3, 1000, "你对他有些了解了：他带着情绪来、讲一件压着他的事、要你开导时，说五六小段，不超过九百字；他这次说的明显还是以前那件事，才连上他以前说过的。"),
         (5, 1100, "你已经挺了解他了：他带着情绪来、讲一件压着他的事、要你开导时，可以把话说透，不超过一千字；说的还是以前那件事时，可以连上他以前说过的、他的习惯。"))


# 伙伴 (pingzuo) sits with him rather than taking the thing apart: a little shorter than 导师 at every level.
# The client wants long, comforting replies "most of the time", so even a new user gets the full length; knowing him adds a little.
DEPTH_PINGZUO = ((1, 680, "你们刚认识，你只知道他刚说的这些：不往外猜他的生活，不装作很懂他。他带着情绪来、讲一件压着他的事、要你开导时，陪他慢慢说四五段，四五百字，最多六百字。"),
                 (3, 780, "你对他有些了解了：他带着情绪来、讲一件压着他的事、要你开导时，陪他慢慢说四五段，不超过七百字；说的还是以前那件事，才轻轻提一句他以前说过的。"),
                 (5, 880, "你已经挺了解他了：他带着情绪来、讲一件压着他的事、要你开导时，陪他慢慢说，不超过八百字；说的还是以前那件事时，可以想起他以前扛过的、他的习惯。"))


def depth(lv, pid=None):
    return next(d for d in (DEPTH_PINGZUO if pid == "pingzuo" else DEPTH) if lv <= d[0])

CONSOLIDATE_PROMPT = """你在帮一个人的“另一个自己”整理记忆。下面有三部分：已经知道的他（画像和记住的事），以及这次新的聊天记录。
读完这次的聊天，输出一个 JSON 对象（只输出 JSON，不要别的字）：
{
  "episode": "这次他说了什么、他是什么状态，一句话，以“你”开头，不超过 50 个字；只写他说的，不写 AI 怎么回的",
  "add": ["这次新知道的、以后值得记住的事，以“你”开头，每条不超过 30 个字，最多 4 条"],
  "drop": ["已记住的事里，被这次的新信息推翻或重复了的条目 id"],
  "portrait": "更新后的“你是怎样的人”，第二人称“你”，不超过 220 个字"
}
写 add 的规则：不写他是跟哪个“自己”说的；只写他亲口说过的事实、处境、在意的人和东西、做过的决定、说过的心愿；带上具体的名字、时间、数字；不写建议，不评价，不写 AI 说了什么；AI 替他说出的心里话和猜测（比如“你心里那句……”“你其实是怕……”）不是他说的，一律不记，拿不准是谁说的就不记；已经记过的不重复写；没有就给空数组。
标着“AI（只作上下文）”的行是 AI 说的：里面替他说的心里话、猜测、比方，都不是他说的，add 和 portrait 都不能用。
标着“他发来的图”的行是图片里抄出来的内容（别人的盘、聊天截图）：可以记“你发过一张……的图”，图上的生日、八字、别人说的话不当成他的事记，除非他自己说了是他的。
写 portrait 的规则：在原来的基础上改，不要推倒重写；写他的处境、在意的人和事、反复出现的心事和情绪、做事和说话的习惯、他常用的词和说法（用引号原样保留几个，只能是标着“他”的行里的原话）；只写聊天里他自己的话有根据的（AI 的猜测和替他说的话不算），不推测，不下诊断，不评判好坏。
如果这次聊天里几乎没有新东西，episode 照写，add 给空数组，portrait 原样返回。"""
# Added to CONSOLIDATE_PROMPT while follows are on (Memory(follow=True))
FOLLOW_PROMPT = """
JSON 里再加两个字段：
  "follow": [{"text": "以“你”开头，什么事、哪天，不超过 30 个字，比如“你11月28日（周六）下午有个面试”", "due": "YYYY-MM-DD，这件事发生的第二天"}],
  "close": ["【等着问的事】里，这次他已经说了结果、或说了不去了/取消了的条目 id"]
写 follow 的规则：只记他亲口说的、一个月内会发生、之后会有结果的具体事：面试、考试、看病复查拿结果、见某人谈某事、交稿截止、搬家、出发旅行之类。聊天记录每段前面标着那段是哪天说的，按那一天算“明天”“后天”“周五”是哪天，due 写这件事发生的第二天；说不出具体哪天的（“过阵子”“最近”）不记。text 里写那天是几月几号、周几，不写“今天”“明天”“后天”“下周”这类过几天就不对的说法。不记：心情和长期的烦恼（“工作好累”“一直担心钱”）；AI 建议他去做的事；已经办完、已经有结果的事；想死、伤害自己一类的话。最多 2 条；【等着问的事】里已有的不重复写；没有就给空数组。
写 close 的规则：只写他这次亲口说了结果（面完了、拿到结果了、谈过了），或说了不去了、取消了、改期还没定的；没有就给空数组。"""
WEEKDAYS = "一二三四五六日"


def valid_uid(uid):
    return isinstance(uid, str) and bool(UID_RE.match(uid))


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Unreadable(OSError):
    """A file of his that is there but can't be read (no permission, a disk error, broken JSON). Nothing may be written
    over it: review 2026-11-26, one failed read of diet.json was taken for "no meals yet", the next meal was written,
    and every earlier meal was gone."""


def _read_json(path):
    """The JSON object in a file; None only when there is no such file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unreadable("%s: %s" % (os.path.basename(path), e)) from e
    if not isinstance(data, dict):
        raise Unreadable("%s: not a JSON object" % os.path.basename(path))
    return data


def _today():
    return datetime.date.today().isoformat()


def _bigrams(s):
    s = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()…—-]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _empty_profile():
    return {"since": _today(), "msgs": 0, "folded": 0, "upto": 0, "portrait": "", "notes": [], "episodes": [],
            "follows": [], "follow_day": None}


def _date(s):
    """A date from 'YYYY-MM-DD…', or None."""
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _follow_pick(prof, today):
    """The one thing to ask him about today, or None: open, today within due..due+2, nothing asked yet today (either
    self); the earliest due first. The red dot (summary) and follow_take both use this, so the dot is what is taken.
    Nothing while some of his lines aren't folded yet: one of them may be the outcome (review 2026-11-27: "面完了" was
    on disk, the fold that would close it still running, and the dot could be taken; the page folds first, /api/sync)."""
    if prof.get("follow_day") == today.isoformat() or prof.get("msgs", 0) > prof.get("folded", 0):
        return None
    due = [(x["due"], i) for i, x in enumerate(prof["follows"]) if x.get("state") == "open" and _date(x.get("due"))
           and 0 <= (today - _date(x["due"])).days < FOLLOW_DAYS]
    return prof["follows"][min(due)[1]] if due else None


RECORD_KINDS = ("diet", "sleep", "mood")
RECORD_DAYS = 14  # what the selves see when he doesn't name a stretch (上个月, 十月, 去年 are looked up as asked)
RECORD_CHARS = 2400  # about this much of one kind at most in a prompt
RECORD_NAMES = {"diet": "饮食", "sleep": "睡眠", "mood": "心情"}
RECORD_USE = ("他问起这些，就照这里说，按天加总、跟前几天比；这里没有的不编。他没问、但这一条正好用得上（比如他在算今天这一顿，"
              "可以顺带说今天一共多少；他说这几天特别困，可以提一句这几晚睡得晚），就轻轻带一句。不念一长串数据，不说教，"
              "他心里正难受的时候不拿这些说事。")
RECORD_HEAD = {"diet": "【他的饮食记录】他让你们算过的每一顿都记在这里（两个“你”都看得到）。" + RECORD_USE,
               "sleep": "【他的睡眠记录】他跟你们说过的每一晚怎么睡的都记在这里，一晚一条（两个“你”都看得到）。" + RECORD_USE,
               "mood": "【他的心情记录】他每天跟你们聊天时的心情，一天一条（箭头是这一天从什么心情变成什么心情；两个“你”都看得到）。"
                       + RECORD_USE}


def parse_kcal(x):
    """[lo, hi] from what a model wrote ([350, 400], "350-400", 380), or None."""
    if isinstance(x, (int, float)) and x > 0:
        return [int(x), int(x)]
    if isinstance(x, str):
        x = [int(n) for n in re.findall(r"\d+", x)][:2]
    if isinstance(x, list) and x and all(isinstance(n, (int, float)) and n >= 0 for n in x[:2]):
        lo, hi = int(x[0]), int(x[1] if len(x) > 1 else x[0])
        return [min(lo, hi), max(lo, hi)] if hi > 0 else None
    return None


def _span(k):
    return "%d" % k[0] if k[0] == k[1] else "%d-%d" % tuple(k)


def _kcal_sum(meals):
    ks = [m["kcal"] for m in meals if m.get("kcal")]
    return _span([sum(k[0] for k in ks), sum(k[1] for k in ks)]) if ks else ""


UNMATCHED = "这是他在更正前面的某一顿，没对上是哪一顿，没算进合计"


def _day_sum(meals):
    """What a day's meals add up to, and what the sums leave out (review 2026-11-25: over a month only the dishes and
    calories were left, so 蛋白质 and "有图没看成" were gone). A correction that fit no meal (unmatched) isn't added in."""
    odd = sum(1 for m in meals if m.get("unmatched"))
    meals = [m for m in meals if not m.get("unmatched")]
    n, out = len(meals), []
    with_kcal = sum(1 for m in meals if m.get("kcal"))
    if with_kcal:
        out.append("记下的一共约 %s 大卡%s" % (_kcal_sum(meals), "（%d 顿里只有 %d 顿算了热量）" % (n, with_kcal) if with_kcal < n else ""))
    for name, f in (("蛋白质", "protein_g"), ("脂肪", "fat_g"), ("碳水", "carb_g")):
        got = [m[f] for m in meals if isinstance(m.get(f), (int, float))]
        if got:
            out.append("%s约 %d 克%s" % (name, sum(got), "（只有 %d 顿算了）" % len(got) if len(got) < n else ""))
    lost = sum(m.get("lost") or 0 for m in meals)
    if lost:
        out.append("有 %d 张图没看成，上面的数没算全" % lost)
    if odd and out:
        out.append("另有 %d 条更正没对上是哪一顿，没算进来" % odd)
    return "，".join(out)


def meal_line(m):
    items = "、".join("%s%s%s" % (i.get("name", ""), " " + i["amount"] if i.get("amount") else "",
                                  " " + _span(i["kcal"]) if i.get("kcal") else "") for i in m.get("items") or [])
    nut = "，".join("%s %d 克" % (k, m[f]) for k, f in (("蛋白质", "protein_g"), ("脂肪", "fat_g"), ("碳水", "carb_g"))
                   if isinstance(m.get(f), (int, float)))
    more = "；".join(x for x in (m.get("note") or "", "有 %d 张图没看成，这一顿可能没算全" % m["lost"] if m.get("lost") else "",
                                 UNMATCHED if m.get("unmatched") else "") if x)
    return "%s（%s 说的）%s%s%s%s" % (m.get("meal") or "一顿", str(m.get("t", ""))[11:16], items,
                                   "，共约 %s 大卡" % _span(m["kcal"]) if m.get("kcal") else "", "，" + nut if nut else "",
                                   "（%s）" % more if more else "")


def sleep_line(r):
    """One night: "23:30 睡，07:10 醒，约 7.5 小时，睡得差（半夜醒了 2 次；做了噩梦）"."""
    parts = []
    if r.get("bed"):
        parts.append(r["bed"] + " 睡")
    if r.get("wake"):
        parts.append(r["wake"] + " 醒")
    if isinstance(r.get("hours"), (int, float)):
        parts.append("约 %g 小时" % r["hours"])
    if r.get("quality"):
        parts.append("睡得" + r["quality"] if r["quality"] != "失眠" else "失眠")
    more = "；".join(x for x in ("半夜醒了 %d 次" % r["wakeups"] if isinstance(r.get("wakeups"), int) and r["wakeups"] > 0 else "",
                                 r.get("note") or "") if x)
    nap = r.get("nap") if isinstance(r.get("nap"), str) else ""
    return "%s%s%s" % ("，".join(parts), "（%s）" % more if more else "", ("；" if parts else "") + nap if nap else "") or "说过睡得怎样"


def mood_line(r):
    """One day: "低落 → 好一点（2 → 4 分，因为加班）"."""
    mood = "%s → %s" % (r["from"], r["mood"]) if r.get("from") else r.get("mood", "")
    more = "，".join(x for x in ("%d 分" % r["score"] if isinstance(r.get("score"), int) else "",
                                "因为" + r["about"] if r.get("about") else "", r.get("note") or "") if x)
    return mood + ("（%s）" % more if more else "")


def merge_day(kind, old, new):
    """Sleep and mood keep one record a day (user 2026-11-25): what he says later fills in or updates that day's record,
    in the order he said it (t), whichever extraction finishes first; the day's mood keeps where it started
    ("from": 低落 -> 好一点)."""
    a, b = (old, new) if str(old.get("t") or "") <= str(new.get("t") or "") else (new, old)
    rec = dict(a, **{k: v for k, v in b.items() if v not in (None, "")})
    if kind == "mood":
        start = a.get("from") or b.get("from") or a.get("mood")
        rec.pop("from", None)
        if start and start != rec.get("mood"):
            rec["from"] = start
    return rec


def parse_json(text):
    """The first {...} in a model's answer as a dict, or None."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Memory:
    def __init__(self, root, llm, follow=False, unsafe=None):
        """llm(messages, max_tokens, temperature) -> str (raises on failure). follow: pick up follows when folding and
        offer them (off: neither). unsafe(text) -> truthy for what is never followed up (the server's crisis words)."""
        self.root = root
        self.llm = llm
        self.follow, self.unsafe = follow, unsafe
        os.makedirs(root, exist_ok=True)
        self.locks = {}
        self.folding = {}  # uid -> Lock: one fold at a time per user (consolidate)
        self.talking = {}  # uid -> turns of his being answered right now (talk): his words aren't on disk yet
        self.wipes = {}  # uid -> times wiped in this process: a fold that was running across a wipe is dropped
        self.rec_clears = {}  # (uid, kind) -> times he cleared that kind (record_ver)
        self.rec_gone = {}  # (uid, kind) -> [(id, d, stub)] of that kind he deleted one by one, in order (record_recent)
        self.glock = threading.Lock()

    # ---------------------------------------------------------------- storage
    def _dir(self, uid):
        if not valid_uid(uid):
            raise ValueError("bad user id")
        return os.path.join(self.root, uid)

    def _lock(self, uid):
        with self.glock:
            return self.locks.setdefault(uid, threading.Lock())

    def _load(self, uid, strict=True):
        """His profile, a new one when he has none yet. One that is there but can't be read raises Unreadable, so nothing
        saves an empty profile over it; strict=False makes do with an empty one, for what only reads it (a prompt)."""
        prof = _empty_profile()
        try:
            saved = _read_json(os.path.join(self._dir(uid), "self.json"))
        except Unreadable as e:
            if strict:
                raise
            sys.stderr.write("profile %s: %s\n" % (uid[:6], e))
            return prof
        if saved is None:
            return prof
        if "folded" not in saved:  # profiles written before the counter existed
            new, _ = self._read_log(uid, saved.get("upto", 0))
            saved["folded"] = saved.get("msgs", 0) - sum(1 for r in new if r.get("role") == "user")
        prof.update(saved)
        return prof

    def _save(self, uid, prof):
        os.makedirs(self._dir(uid), exist_ok=True)
        write_json_atomic(os.path.join(self._dir(uid), "self.json"), prof)

    def _read_log(self, uid, start):
        """Records from line `start` on, and how many raw lines that was (bad lines still count)."""
        try:
            with open(os.path.join(self._dir(uid), "log.jsonl"), encoding="utf-8") as f:
                lines = f.readlines()[start:]
        except OSError:
            return [], 0
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out, len(lines)

    # ---------------------------------------------------------------- writing
    def append(self, uid, pid, recs, gen=None):
        """recs: [(role, text)], role in user / ai / card / closing. Returns how many user lines wait to be folded.
        gen: self.gen(uid) when the request came in; if he wiped his data since, nothing is written (review 2026-11-25:
        a reply that came back after 全部清空 put his words back)."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock(uid):
            if gen is not None and self.gen(uid) != gen:
                return 0
            prof = self._load(uid)
            os.makedirs(self._dir(uid), exist_ok=True)
            with open(os.path.join(self._dir(uid), "log.jsonl"), "a", encoding="utf-8") as f:
                for role, text in recs:
                    if text:
                        f.write(json.dumps({"t": now, "p": pid, "role": role, "text": text}, ensure_ascii=False) + "\n")
                        if role == "user":
                            prof["msgs"] += 1
            self._save(uid, prof)
        return prof["msgs"] - prof["folded"]

    def import_notes(self, uid, notes):
        """Seed from the notes an older version of the page kept in localStorage (only when empty)."""
        with self._lock(uid):
            prof = self._load(uid)
            if prof["notes"]:
                return 0
            prof["notes"] = [{"id": secrets.token_hex(4), "text": str(t).strip()[:40], "d": _today()}
                             for t in notes[:30] if str(t).strip()]
            self._save(uid, prof)
            return len(prof["notes"])

    def forget(self, uid, note_id):
        with self._lock(uid):
            prof = self._load(uid)
            n = len(prof["notes"])
            prof["notes"] = [x for x in prof["notes"] if x.get("id") != note_id]
            self._save(uid, prof)
            return n != len(prof["notes"])

    def wipe(self, uid):
        """Delete everything of his, as much as can be. -> what is still there, [] when all of it is gone: "memory" (what
        the selves know, what he said) and the record kinds. The page tells him (review 2026-11-25: a failed delete was
        reported as done; 11-26: when part of it went, the page said nothing had been deleted). Work begun before is
        dropped either way."""
        with self._lock(uid):
            with self.glock:
                self.wipes[uid] = self.wipes.get(uid, 0) + 1
                for k in RECORD_KINDS:  # what was kept of the meals he deleted one by one goes too (review 2026-11-26)
                    self.rec_gone.pop((uid, k), None)
            try:
                shutil.rmtree(self._dir(uid), ignore_errors=True)  # one file that won't go doesn't keep the rest
            except OSError as e:
                sys.stderr.write("wipe %s: %s\n" % (uid[:6], e))
            d = self._dir(uid)
            if not os.path.exists(d):
                return []
            left = [k for k in RECORD_KINDS if os.path.exists(self._rec_path(uid, k))]
            if not left or any(os.path.exists(os.path.join(d, f)) for f in ("self.json", "log.jsonl")):
                left.insert(0, "memory")
            sys.stderr.write("wipe %s: left %s\n" % (uid[:6], left))
            return left

    def gen(self, uid):
        """Changes when his data is wiped: what a request begun before that would write is dropped."""
        with self.glock:
            return self.wipes.get(uid, 0)

    def record_ver(self, uid, kind):
        """Taken when he sends a turn, checked when what was pulled out of it is written (record_put): nothing goes
        into a kind he cleared since, or onto a record of it he deleted since (review 2026-11-25: a cleared mood came
        back from a running extraction; then deleting an old meal dropped a new one too)."""
        with self.glock:
            return (self.wipes.get(uid, 0), self.rec_clears.get((uid, kind), 0),
                    len(self.rec_gone.get((uid, kind), ())))

    def record_fresh(self, uid, kind, ver):
        """Neither wiped nor that kind cleared since `ver` (record_ver when he sent the turn)."""
        return self.record_ver(uid, kind)[:2] == tuple(ver[:2])

    # ---------------------------------------------------------------- 专项记录: 饮食, 睡眠
    # Client 2026-11-25: meals he has the selves work out (a photo of a meal and "计算一下卡路里") are kept for good in
    # their own record, and so are the nights he tells them about; what to do with them is not settled yet, but the
    # selves must be able to look them up (record_block), and he can see and clear them himself (/api/records).
    def _rec_path(self, uid, kind):
        if kind not in RECORD_KINDS:
            raise ValueError("unknown record kind")
        return os.path.join(self._dir(uid), kind + ".json")

    def _rec_load(self, uid, kind):
        """His records of one kind, [] only when he has none (Unreadable when the file is there but can't be read)."""
        items = (_read_json(self._rec_path(uid, kind)) or {}).get("items", [])
        if not isinstance(items, list):
            raise Unreadable("%s.json: no list of items" % kind)
        return items

    def _rec_write(self, uid, kind, items):
        os.makedirs(self._dir(uid), exist_ok=True)
        write_json_atomic(self._rec_path(uid, kind), {"items": items})

    def records(self, uid, kind):
        """His records of one kind, oldest first (Unreadable: the page says it can't get them, not that there are none)."""
        with self._lock(uid):
            return self._rec_load(uid, kind)

    def record_kinds(self, uid):
        """The kinds he has records of (only those are looked up); one that can't be read counts, record_block says so."""
        def has(k):
            try:
                return bool(self.records(uid, k))
            except Unreadable:
                return True
        return [k for k in RECORD_KINDS if has(k)]

    def record_said(self, uid, kind, ver, keep, n=None):
        """His records for which keep(r), oldest first and, said at the same moment, in the order they were written (two
        meals from one line keep theirs); the last n. A follow-up about one of them updates it. ver = record_ver when he
        sent the turn: [] when he has cleared that kind or wiped since, so nothing of his goes to the model again. One
        he deleted since is there only as {id, t, d, meal, gone: true}, without what was in it, so a correction of it is
        dropped by record_put instead of coming back as a new meal (review 2026-11-26: "那碗拌面其实是二两半", then the
        meal was deleted before the extraction ran). A correction that fit no meal (unmatched) isn't one."""
        with self._lock(uid):
            if not self.record_fresh(uid, kind, ver):
                return []
            items = [r for r in self._rec_load(uid, kind) if not r.get("unmatched")]
            with self.glock:
                gone = [stub for _, _, stub in self.rec_gone.get((uid, kind), [])[ver[2]:]]
        said = sorted((str(r.get("t") or ""), i, r) for i, r in enumerate(items + gone) if keep(r))
        return [r for _, _, r in said][-n if n else 0:]

    def record_recent(self, uid, kind, hours, now, ver, n=4, days=0):
        """record_said: the ones he told in the `hours` before `now` (when he sent the turn), and with days the ones of
        the last `days` days (today, yesterday) told before then too."""
        upto = now.isoformat(timespec="seconds")
        since = (now - datetime.timedelta(hours=hours)).isoformat(timespec="seconds")
        dates = {(now.date() - datetime.timedelta(days=k)).isoformat() for k in range(days)}
        return self.record_said(uid, kind, ver, lambda r: str(r.get("t") or "") <= upto and
                                (since <= str(r.get("t") or "") or r.get("d") in dates), n)

    def record_put(self, uid, kind, rec, ver, same=None):
        """Write what was pulled out of one turn, finding and merging under one hold of his lock: a meal (in place of
        the one with id `same` when it is a follow-up about it), a night or a day's mood (merged into that day's record:
        one a day, in the order he said things). -> the record written, or None when `ver` (record_ver when he sent the
        turn) is out of date."""
        with self._lock(uid):
            if not self.record_fresh(uid, kind, ver):  # wiped, or this kind cleared
                return None
            with self.glock:
                gone = self.rec_gone.get((uid, kind), [])[ver[2]:]
            if any(rid == same if kind == "diet" else d == rec["d"] for rid, d, _ in gone):
                return None  # deleted since: the meal he followed up on, or that day's night or mood
            items = self._rec_load(uid, kind)
            if kind == "diet":
                i = next((i for i, r in enumerate(items) if same and r.get("id") == same), None)
                if i is not None:  # the same meal, worked out again: it stays on its day, under its name; a picture
                    old = items[i]  # that couldn't be read stays missing until he sends pictures again
                    rec = dict(rec, d=old.get("d", rec["d"]), meal=rec.get("meal") or old.get("meal"),
                               pics=max(rec.get("pics") or 0, old.get("pics") or 0),
                               lost=rec.get("lost") if rec.get("pics") or rec.get("lost") else old.get("lost"))
            else:
                i = next((i for i in range(len(items) - 1, -1, -1) if items[i].get("d") == rec["d"]), None)
                if i is not None:
                    rec = merge_day(kind, items[i], rec)
            if i is None:
                items.append(dict(rec, id=secrets.token_hex(4)))
            else:
                items[i] = dict(rec, id=items[i]["id"])
            self._rec_write(uid, kind, items)
            return items[-1] if i is None else items[i]

    def record_delete(self, uid, kind, rid):
        """-> whether it was there (OSError when it couldn't be written)."""
        with self._lock(uid):
            items = self._rec_load(uid, kind)
            gone = [r for r in items if r.get("id") == rid]
            if not gone:
                return False
            self._rec_write(uid, kind, [r for r in items if r.get("id") != rid])
            with self.glock:
                # only enough to know a follow-up was about it (record_recent): not what he ate
                stub = {"id": rid, "t": gone[0].get("t"), "d": gone[0].get("d"), "meal": gone[0].get("meal"), "gone": True}
                self.rec_gone.setdefault((uid, kind), []).append((rid, gone[0].get("d"), stub))
            return True

    def record_clear(self, uid, kind):
        """-> False when the file is still there."""
        with self._lock(uid):
            try:
                os.remove(self._rec_path(uid, kind))
            except FileNotFoundError:
                pass
            except OSError as e:
                sys.stderr.write("record_clear %s %s: %s\n" % (uid[:6], kind, e))
                return False
            with self.glock:
                self.rec_clears[(uid, kind)] = self.rec_clears.get((uid, kind), 0) + 1
                self.rec_gone.pop((uid, kind), None)
            return True

    def record_block(self, uid, kind, since=None, until=None):
        """【他的饮食记录】/【他的睡眠记录】/【他的心情记录】 for the system prompt, newest day first: the last RECORD_DAYS days,
        or the days from `since` to `until` (dates) when he asked about a stretch (records_needed). It says which days
        it covers and how far back his records go, so a self can tell "没记过" from "这里没列"."""
        today = datetime.date.today()
        until = min(until or today, today)
        since = min(since or until - datetime.timedelta(days=RECORD_DAYS - 1), until)
        by_day, days = {}, set()
        try:
            items = self.records(uid, kind)
        except Unreadable as e:
            sys.stderr.write("records %s: %s\n" % (uid[:6], e))
            return "【他的%s记录】这会儿读不出来。他问起，就说现在查不到、过一会儿再看；不要说他没记过，也不要编。" % RECORD_NAMES[kind]
        for r in items:
            try:
                d = datetime.date.fromisoformat(r["d"])
            except (KeyError, ValueError, TypeError):
                continue
            days.add(d)
            if since <= d <= until:
                by_day.setdefault(d, []).append(r)
        if not days:
            return ""
        brief = (until - since).days >= RECORD_DAYS  # a long stretch: the meals of a day, not every dish
        out, size, left = [RECORD_HEAD[kind]], 0, 0
        for d in sorted(by_day, reverse=True):
            if size > RECORD_CHARS:
                left += 1
                continue
            day = "%s%s" % (d.isoformat()[5:] if d.year == today.year else d.isoformat(),
                            {0: "（今天）", 1: "（昨天）"}.get((today - d).days, ""))
            if kind == "diet":
                meals = "；".join("%s：%s%s%s" % (r.get("meal") or "一顿", "、".join(i.get("name", "") for i in r.get("items") or []),
                                                 "（约 %s 大卡）" % _span(r["kcal"]) if r.get("kcal") else "",
                                                 "（%s）" % UNMATCHED if r.get("unmatched") else "")
                                 if brief else meal_line(r) for r in by_day[d])
                total = _day_sum(by_day[d]) if brief or len(by_day[d]) > 1 else ""
                line = "%s：%s%s" % (day, meals, "。这天" + total if total else "")
            else:
                each = sleep_line if kind == "sleep" else mood_line
                line = "%s%s：%s" % (day, "夜里" if kind == "sleep" else "", "；".join(each(r) for r in by_day[d]))
            line = line if len(line) <= RECORD_CHARS // 4 else line[:RECORD_CHARS // 4] + "……"
            out.append(line)
            size += len(line)
        span = "%s 到 %s" % (since.isoformat(), until.isoformat())
        if not by_day:
            out.append("%s 这段没有记录。" % span)
        elif left:
            out.append("（%s 这段里，再往前还有 %d 天的没列出来）" % (span, left))
        out.append("（这里列的是 %s 的；他的%s记录从 %s 记到 %s，一共 %d 天有记。）"
                   % (span, RECORD_NAMES[kind], min(days).isoformat(), max(days).isoformat(), len(days)))
        return "\n".join(out)

    # ---------------------------------------------------------------- folding in
    def consolidate(self, uid, min_user=2, wait=0):
        """Fold unconsolidated lines into the profile. Returns {"add": [...], "episode": str} or None. One fold at a time
        per user: with wait (seconds) a second one waits for the running one, then folds what is still left (often
        nothing: no model call); without, it gives up at once (the background folds). /api/sync waits, so the home page
        it is followed by sees the fold done (review 2026-11-27: it returned at once while a background fold ran, and the
        red dot that fold freed never showed)."""
        with self.glock:
            lock = self.folding.setdefault(uid, threading.Lock())
        if not (lock.acquire(timeout=wait) if wait else lock.acquire(blocking=False)):
            return None
        try:
            with self._lock(uid):
                gen = self.wipes.get(uid, 0)
                prof = self._load(uid)
                if prof["msgs"] - prof["folded"] < min_user:
                    return None
                new, nlines = self._read_log(uid, prof["upto"])
            n_user = sum(1 for r in new if r.get("role") == "user")
            today = datetime.date.today()
            convo, day = [], None
            for r in new[-60:]:  # each day's lines under that day: "明天" said yesterday is today
                d = _date(r.get("t"))
                if d and d != day:
                    convo.append("【%s 周%s 说的】" % (d.isoformat(), WEEKDAYS[d.weekday()]))
                    day = d
                convo.append(self._line(r, 300))
            said = _date(new[-60:][0].get("t")) if new else None
            known = ["【今天】%s 周%s" % (today.isoformat(), WEEKDAYS[today.weekday()]),
                     "【他是怎样的人】", prof["portrait"] or "（还不了解）", "【已经记住的事】"]
            known += ["%s｜%s" % (x["id"], x["text"]) for x in prof["notes"][:60]] or ["（还没有）"]
            if self.follow:
                known += ["【等着问的事】"] + (["%s｜%s｜%s" % (x["id"], x["text"], x["due"]) for x in prof["follows"]
                                              if x.get("state") == "open"] or ["（没有）"])
            msgs = [{"role": "system", "content": CONSOLIDATE_PROMPT + (FOLLOW_PROMPT if self.follow else "")},
                    {"role": "user", "content": "\n".join(known) + "\n\n【这次的聊天】\n" + "\n".join(convo)}]
            data = parse_json(self.llm(msgs, 900 if self.follow else 700, 0.3))
            if data is None:
                return None
            pid = new[-1].get("p") if new else None
            last = _date(new[-1].get("t")) if new else None
            with self._lock(uid):
                if self.wipes.get(uid, 0) != gen:  # he cleared what the selves know while this fold was running
                    return None
                prof = self._load(uid)
                drop = {str(x) for x in data.get("drop") or []}
                have = {x["text"] for x in prof["notes"]}
                added = []
                for t in data.get("add") or []:
                    t = re.sub(r"^(用户|他|她)", "你", str(t).strip()).replace("用户", "你")[:40]
                    if t and t not in have:
                        added.append({"id": secrets.token_hex(4), "text": t, "d": _today(), "p": pid})
                prof["notes"] = (added + [x for x in prof["notes"] if x["id"] not in drop])[:NOTE_CAP]
                ep = str(data.get("episode") or "").strip()[:80]
                if ep:  # dated by the talk, not the fold: yesterday's talk folded today is still "昨天" to him
                    prof["episodes"] = (prof["episodes"] + [{"d": (last or today).isoformat(), "p": pid, "text": ep}]
                                        )[-EPISODE_CAP:]
                pt = str(data.get("portrait") or "").strip()
                if 20 <= len(pt) <= 400:
                    prof["portrait"] = pt
                if self.follow:
                    self._fold_follows(prof, data, said or today, today, pid)
                prof["upto"] += nlines
                prof["folded"] += n_user
                self._save(uid, prof)
            return {"add": [x["text"] for x in added], "episode": ep}
        finally:
            lock.release()

    @contextmanager
    def talk(self, uid):
        """Around a chat turn (App.chat): his words go on disk only with the reply, and they may be the outcome of
        something waiting to be asked (review 2026-11-27: "面试过了" sent, home before the reply, the dot still taken)."""
        if uid:
            with self.glock:
                self.talking[uid] = self.talking.get(uid, 0) + 1
        try:
            yield
        finally:
            if uid:
                with self.glock:
                    self.talking[uid] -= 1
                    if not self.talking[uid]:
                        del self.talking[uid]

    def _talking(self, uid):
        with self.glock:
            return bool(self.talking.get(uid))

    def _fold_follows(self, prof, data, said, today, pid):
        """A fold's "close" and "follow" into prof["follows"] (under the lock). said = the day of the fold's first line:
        a due must come after it and within FOLLOW_AHEAD days. What the model gets wrong is dropped, not guessed at."""
        close = data.get("close") if isinstance(data.get("close"), list) else []
        for x in prof["follows"]:
            if x.get("state") == "open" and x["id"] in {str(c) for c in close}:
                x["state"] = "closed"
        have = {x["text"] for x in prof["follows"]}
        got = data.get("follow") if isinstance(data.get("follow"), list) else []
        for f in [f for f in got if isinstance(f, dict)][:2]:
            text = re.sub(r"^(用户|他|她)", "你", str(f.get("text") or "").strip()).replace("用户", "你")[:40]
            due = _date(f.get("due"))
            if not text or text in have or (self.unsafe and self.unsafe(text)) or not due \
                    or not said < due <= said + datetime.timedelta(days=FOLLOW_AHEAD):
                sys.stderr.write("follow dropped: %r\n" % (f,))
                continue
            prof["follows"].append({"id": secrets.token_hex(4), "text": text, "due": due.isoformat(), "p": pid,
                                    "state": "open"})
            have.add(text)
        # past their days nothing can ask about them; then at most FOLLOW_CAP open ones, the latest kept
        keep = [x for x in prof["follows"] if _date(x.get("due")) and (today - _date(x["due"])).days < FOLLOW_DAYS]
        opens = sorted((x for x in keep if x.get("state") == "open"), key=lambda x: x["due"])
        gone = {x["id"] for x in opens[:max(0, len(opens) - FOLLOW_CAP)]}
        prof["follows"] = [x for x in keep if x["id"] not in gone]

    def follow_take(self, uid, pid, fid, today=None):
        """He tapped the red dot on self `pid`'s card for follow `fid`: if that is still the one to ask today and it is
        this self's, mark it asked (and today used) and return it with "day" (the day it used) and "prev" (follow_day
        before, for follow_undo); otherwise None. Under the lock: the other card, an old id or a second tab gets nothing."""
        if not self.follow:
            return None
        today = today or datetime.date.today()
        if self._talking(uid):
            return None
        with self._lock(uid):
            prof = self._load(uid)
            f = _follow_pick(prof, today)
            if not f or f["id"] != fid or f.get("p") != pid:
                return None
            prev, f["state"], prof["follow_day"] = prof.get("follow_day"), "asked", today.isoformat()
            self._save(uid, prof)
            return dict(f, prev=prev, day=today.isoformat())

    def follow_undo(self, uid, f):
        """The opening for follow `f` (from follow_take) couldn't be made: open again; its day is given back only if it
        is still the day used (review 2026-11-27: taken before midnight, failed after another was taken the next day,
        and the next day's one-a-day was given back)."""
        with self._lock(uid):
            prof = self._load(uid)
            x = next((x for x in prof["follows"] if x["id"] == f["id"] and x.get("state") == "asked"), None)
            if x:  # gone (wiped since): nothing to put back
                x["state"] = "open"
                if prof.get("follow_day") == f.get("day"):  # one a day: that day's one was this
                    prof["follow_day"] = f.get("prev")
                self._save(uid, prof)

    # ---------------------------------------------------------------- reading
    @staticmethod
    def level(prof):
        days = (datetime.date.today() - datetime.date.fromisoformat(prof["since"])).days + 1
        score = len(prof["notes"]) + 0.5 * len(prof["episodes"]) + 0.3 * min(days, 60)
        return sum(1 for t in LEVELS if score >= t), days

    def summary(self, uid, strict=False, today=None):
        """strict: Unreadable instead of an empty profile (the page keeps what it showed)."""
        with self._lock(uid):
            prof = self._load(uid, strict)
        lv, days = self.level(prof)
        f = _follow_pick(prof, today or datetime.date.today()) if self.follow and not self._talking(uid) else None
        return {"days": days, "msgs": prof["msgs"], "level": lv, "portrait": prof["portrait"],
                "notes": [{"id": x["id"], "text": x["text"], "d": x["d"]} for x in prof["notes"]],
                "episodes": prof["episodes"][-10:],
                "due": f and {"id": f["id"], "p": f.get("p")}}  # the red dot: which card, never what it is about

    def reply_cap(self, uid, pid=None):
        """Most 字 a chat reply from self `pid` may have for this user (see DEPTH)."""
        with self._lock(uid):
            prof = self._load(uid, strict=False)
        return depth(self.level(prof)[0], pid)[1]

    def _line(self, r, n):
        """One transcript line. The selves' lines are marked as AI context, so their guesses about him are not mistaken
        for things he said, and never carry a self's name (the selves must not seem to know about each other)."""
        who = {"user": "他", "card": "彩虹卡"}.get(r.get("role")) or "AI（只作上下文）"  # no self's name: which self heard it is never shown
        text = " ".join(str(r.get("text", "")).split())
        if who == "他" and text.startswith("【图片】"):  # a chart or chat screenshot he sent may be someone else's
            who = "他发来的图（图上的出生信息、别人说的话不算他的事，他自己说明了才算）"
        return "%s：%s" % (who, text[:n])

    def just_said(self, uid, minutes=60):
        """His lines (to either self, folded or not) from the last `minutes`, oldest first, at most 6."""
        since = (datetime.datetime.now() - datetime.timedelta(minutes=minutes)).isoformat(timespec="seconds")
        with self._lock(uid):
            recs = self._read_log(uid, -40)[0]
        return [str(r.get("text", "")) for r in recs if r.get("role") == "user" and str(r.get("t", "")) >= since][-6:]

    def context(self, uid, recent_user_text, pid=None, brief=False):
        """The block that goes into the system prompt: who this person is, as far as the selves know. Both selves
        share it; `pid` is the one he is talking to, and what he said to the other one since the last fold is added
        verbatim, so switching selves never meets a stranger (pid=None adds every unfolded line). brief: only how long
        they have known each other, for talk that shouldn't be pulled back to his old topics (star signs and the like)."""
        # this self's own unfolded lines are in the history the page sends only for this visit; older ones (yesterday's,
        # never folded: one line and gone) would be lost to it while the other self sees them (review 2026-11-27)
        visit = (datetime.datetime.now() - SESSION_GAP).isoformat(timespec="seconds")
        with self._lock(uid):
            prof = self._load(uid, strict=False)
            # only his lines (and cards): the other self's replies were being paraphrased, so both sounded the same
            fresh = [r for r in self._read_log(uid, prof["upto"])[0] if r.get("role") in ("user", "card")
                     and (r.get("p") != pid or str(r.get("t", "")) < visit)][-8:]
        lv, days = self.level(prof)
        out = ["【你对他的了解】（都是他告诉你的，他说过的才算数；跟他现在说的对不上，就以现在说的为准，不在回复里琢磨、不点破）"
               "这些是让你懂他的，不是每次都要提：他说起新的事、换了话题，就聊他现在说的，不把以前的事扯进来；"
               "他自己提起以前的事，或者说的明显还是同一件，才接上。",
               "认识第 %d 天，他一共说过 %d 句话。" % (days, prof["msgs"])]
        if brief:
            return "\n".join(out)
        if prof["msgs"] == 0:
            out.append(NEW_USER_LINE)
        out.append(depth(lv, pid)[2])
        if prof["portrait"]:
            out.append("他是怎样的人：" + prof["portrait"])
        if prof["notes"]:
            out.append("记得的事（新的在前）：")
            out += ["- %s（%s）" % (x["text"], x["d"][5:]) for x in prof["notes"][:40]]
        eps = prof["episodes"]
        if eps:
            out.append("最近几次聊天：")
            out += ["- %s：%s" % (e["d"][5:], e["text"]) for e in eps[-5:]]
            older, q = eps[:-5], _bigrams(recent_user_text)
            if older and q:
                scored = sorted(((len(q & _bigrams(e["text"])), i) for i, e in enumerate(older)), reverse=True)
                hits = [older[i] for s, i in scored[:3] if s >= 2]
                if hits:
                    out.append("可能跟这次有关的往事：")
                    out += ["- %s：%s" % (e["d"], e["text"]) for e in hits]
        if fresh:
            out.append("他最近还说过这些，还没整理进上面（最后一句在 %s）："
                       % str(fresh[-1].get("t", ""))[5:16].replace("T", " "))
            out += [self._line(r, 150) for r in fresh]
        return "\n".join(out)
