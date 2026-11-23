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
      episodes   one line per talk, max 300
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
import threading

UID_RE = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
NOTE_CAP, EPISODE_CAP = 80, 300
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
写 portrait 的规则：在原来的基础上改，不要推倒重写；写他的处境、在意的人和事、反复出现的心事和情绪、做事和说话的习惯、他常用的词和说法（用引号原样保留几个，只能是标着“他”的行里的原话）；只写聊天里他自己的话有根据的（AI 的猜测和替他说的话不算），不推测，不下诊断，不评判好坏。
如果这次聊天里几乎没有新东西，episode 照写，add 给空数组，portrait 原样返回。"""


def valid_uid(uid):
    return isinstance(uid, str) and bool(UID_RE.match(uid))


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _today():
    return datetime.date.today().isoformat()


def _bigrams(s):
    s = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()…—-]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _empty_profile():
    return {"since": _today(), "msgs": 0, "folded": 0, "upto": 0, "portrait": "", "notes": [], "episodes": []}


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
    def __init__(self, root, llm):
        """llm(messages, max_tokens, temperature) -> str (raises on failure)."""
        self.root = root
        self.llm = llm
        os.makedirs(root, exist_ok=True)
        self.locks = {}
        self.busy = set()
        self.glock = threading.Lock()

    # ---------------------------------------------------------------- storage
    def _dir(self, uid):
        if not valid_uid(uid):
            raise ValueError("bad user id")
        return os.path.join(self.root, uid)

    def _lock(self, uid):
        with self.glock:
            return self.locks.setdefault(uid, threading.Lock())

    def _load(self, uid):
        prof = _empty_profile()
        try:
            with open(os.path.join(self._dir(uid), "self.json"), encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
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
    def append(self, uid, pid, recs):
        """recs: [(role, text)], role in user / ai / card / closing. Returns how many user lines wait to be folded."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock(uid):
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
        with self._lock(uid):
            shutil.rmtree(self._dir(uid), ignore_errors=True)

    # ---------------------------------------------------------------- folding in
    def consolidate(self, uid, min_user=2):
        """Fold unconsolidated lines into the profile. Returns {"add": [...], "episode": str} or None."""
        with self.glock:
            if uid in self.busy:
                return None
            self.busy.add(uid)
        try:
            with self._lock(uid):
                prof = self._load(uid)
                if prof["msgs"] - prof["folded"] < min_user:
                    return None
                new, nlines = self._read_log(uid, prof["upto"])
            n_user = sum(1 for r in new if r.get("role") == "user")
            convo = []
            for r in new[-60:]:
                convo.append(self._line(r, 300))
            known = ["【他是怎样的人】", prof["portrait"] or "（还不了解）", "【已经记住的事】"]
            known += ["%s｜%s" % (x["id"], x["text"]) for x in prof["notes"][:60]] or ["（还没有）"]
            msgs = [{"role": "system", "content": CONSOLIDATE_PROMPT},
                    {"role": "user", "content": "\n".join(known) + "\n\n【这次的聊天】\n" + "\n".join(convo)}]
            data = parse_json(self.llm(msgs, 700, 0.3))
            if data is None:
                return None
            pid = new[-1].get("p") if new else None
            with self._lock(uid):
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
                if ep:
                    prof["episodes"] = (prof["episodes"] + [{"d": _today(), "p": pid, "text": ep}])[-EPISODE_CAP:]
                pt = str(data.get("portrait") or "").strip()
                if 20 <= len(pt) <= 400:
                    prof["portrait"] = pt
                prof["upto"] += nlines
                prof["folded"] += n_user
                self._save(uid, prof)
            return {"add": [x["text"] for x in added], "episode": ep}
        finally:
            with self.glock:
                self.busy.discard(uid)

    # ---------------------------------------------------------------- reading
    @staticmethod
    def level(prof):
        days = (datetime.date.today() - datetime.date.fromisoformat(prof["since"])).days + 1
        score = len(prof["notes"]) + 0.5 * len(prof["episodes"]) + 0.3 * min(days, 60)
        return sum(1 for t in LEVELS if score >= t), days

    def summary(self, uid):
        with self._lock(uid):
            prof = self._load(uid)
        lv, days = self.level(prof)
        return {"days": days, "msgs": prof["msgs"], "level": lv, "portrait": prof["portrait"],
                "notes": [{"id": x["id"], "text": x["text"], "d": x["d"]} for x in prof["notes"]],
                "episodes": prof["episodes"][-10:]}

    def reply_cap(self, uid, pid=None):
        """Most 字 a chat reply from self `pid` may have for this user (see DEPTH)."""
        with self._lock(uid):
            prof = self._load(uid)
        return depth(self.level(prof)[0], pid)[1]

    def _line(self, r, n):
        """One transcript line. The selves' lines are marked as AI context, so their guesses about him are not mistaken
        for things he said, and never carry a self's name (the selves must not seem to know about each other)."""
        who = {"user": "他", "card": "彩虹卡"}.get(r.get("role")) or "AI（只作上下文）"  # no self's name: which self heard it is never shown
        return "%s：%s" % (who, " ".join(str(r.get("text", "")).split())[:n])

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
        with self._lock(uid):
            prof = self._load(uid)
            # only his lines (and cards): the other self's replies were being paraphrased, so both sounded the same
            fresh = [r for r in self._read_log(uid, prof["upto"])[0]
                     if r.get("p") != pid and r.get("role") in ("user", "card")][-8:]
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
