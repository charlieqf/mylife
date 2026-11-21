"""Long-term memory: each user's "other selves" get to know them a little more after every talk.

Per anonymous user id (random, made in the browser, kept in localStorage; no account):
  /var/cache/mylife/users/<uid>/log.jsonl     every line said (user, persona replies, closing lines)
  /var/cache/mylife/users/<uid>/self.json     what the two selves know about the user:
      since      first day
      msgs       how many things the user has said in total
      upto       how many log lines have been folded into the profile
      portrait   "你是怎样的人" (<=220 字, second person), rewritten as they learn more
      notes      facts worth remembering ("你……", <=30 字 each, newest first, max 80)
      episodes   one line per talk, max 300
Folding new lines in ("consolidate") is one LLM call that returns JSON. It runs in the background
after every 8 new user lines, and synchronously from /api/sync when the user leaves a chat.
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

CONSOLIDATE_PROMPT = """你在帮一个人的“另一个自己”整理记忆。下面有三部分：已经知道的他（画像和记住的事），以及这次新的聊天记录。
读完这次的聊天，输出一个 JSON 对象（只输出 JSON，不要别的字）：
{
  "episode": "这次聊了什么、他是什么状态，一句话，以“你”开头，不超过 50 个字",
  "add": ["这次新知道的、以后值得记住的事，以“你”开头，每条不超过 30 个字，最多 4 条"],
  "drop": ["已记住的事里，被这次的新信息推翻或重复了的条目 id"],
  "portrait": "更新后的“你是怎样的人”，第二人称“你”，不超过 220 个字"
}
写 add 的规则：只写他亲口说过的事实、处境、在意的人和东西、做过的决定、说过的心愿；带上具体的名字、时间、数字；不写建议，不评价，不写 AI 说了什么；已经记过的不重复写；没有就给空数组。
写 portrait 的规则：在原来的基础上改，不要推倒重写；写他的处境、在意的人和事、反复出现的心事和情绪、做事和说话的习惯、他常用的词和说法（用引号原样保留几个）；只写聊天里有根据的，不推测，不下诊断，不评判好坏。
如果这次聊天里几乎没有新东西，episode 照写，add 给空数组，portrait 原样返回。"""


def valid_uid(uid):
    return isinstance(uid, str) and bool(UID_RE.match(uid))


def _today():
    return datetime.date.today().isoformat()


def _bigrams(s):
    s = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()…—-]+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


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
        try:
            with open(os.path.join(self._dir(uid), "self.json"), encoding="utf-8") as f:
                prof = json.load(f)
        except (OSError, ValueError):
            prof = {}
        prof.setdefault("since", _today())
        prof.setdefault("msgs", 0)
        prof.setdefault("upto", 0)
        prof.setdefault("logn", 0)
        prof.setdefault("portrait", "")
        prof.setdefault("notes", [])
        prof.setdefault("episodes", [])
        return prof

    def _save(self, uid, prof):
        d = self._dir(uid)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "self.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, "self.json"))

    def _read_log(self, uid, start):
        try:
            with open(os.path.join(self._dir(uid), "log.jsonl"), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for ln in lines[start:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    # ---------------------------------------------------------------- writing
    def append(self, uid, pid, recs):
        """recs: [(role, text)], role in user / ai / closing. Returns unconsolidated user lines."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock(uid):
            prof = self._load(uid)
            d = self._dir(uid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8") as f:
                for role, text in recs:
                    if text:
                        f.write(json.dumps({"t": now, "p": pid, "role": role, "text": text}, ensure_ascii=False) + "\n")
                        prof["logn"] += 1
                        if role == "user":
                            prof["msgs"] += 1
            self._save(uid, prof)
            pending = [r for r in self._read_log(uid, prof["upto"]) if r.get("role") == "user"]
        return len(pending)

    def import_notes(self, uid, notes):
        """Seed from the notes an older version of the page kept in localStorage (only when empty)."""
        with self._lock(uid):
            prof = self._load(uid)
            if prof["notes"]:
                return 0
            for t in notes[:30]:
                t = str(t).strip()[:40]
                if t:
                    prof["notes"].append({"id": secrets.token_hex(4), "text": t, "d": _today()})
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
                new = self._read_log(uid, prof["upto"])
                end = prof["upto"] + len(new)
            if sum(1 for r in new if r.get("role") == "user") < min_user:
                return None
            names = {"bange": "半格", "pingzuo": "平坐"}
            convo = []
            for r in new[-60:]:
                who = "他" if r.get("role") == "user" else names.get(r.get("p"), "AI")
                convo.append("%s：%s" % (who, str(r.get("text", ""))[:300]))
            known = ["【他是怎样的人】", prof["portrait"] or "（还不了解）", "【已经记住的事】"]
            known += ["%s｜%s" % (x["id"], x["text"]) for x in prof["notes"][:60]] or ["（还没有）"]
            msgs = [{"role": "system", "content": CONSOLIDATE_PROMPT},
                    {"role": "user", "content": "\n".join(known) + "\n\n【这次的聊天】\n" + "\n".join(convo)}]
            data = self._parse(self.llm(msgs, 700, 0.3))
            if data is None:
                return None
            with self._lock(uid):
                prof = self._load(uid)
                drop = {str(x) for x in data.get("drop") or []}
                have = {x["text"] for x in prof["notes"]}
                added = []
                for t in data.get("add") or []:
                    t = re.sub(r"^(用户|他|她)", "你", str(t).strip()).replace("用户", "你")[:40]
                    if t and t not in have:
                        added.append({"id": secrets.token_hex(4), "text": t, "d": _today(), "p": new[-1].get("p")})
                prof["notes"] = (added + [x for x in prof["notes"] if x["id"] not in drop])[:NOTE_CAP]
                ep = str(data.get("episode") or "").strip()[:80]
                if ep:
                    prof["episodes"] = (prof["episodes"] + [{"d": _today(), "p": new[-1].get("p"), "text": ep}])[-EPISODE_CAP:]
                pt = str(data.get("portrait") or "").strip()
                if 20 <= len(pt) <= 400:
                    prof["portrait"] = pt
                prof["upto"] = end
                self._save(uid, prof)
            return {"add": [x["text"] for x in added], "episode": ep}
        finally:
            with self.glock:
                self.busy.discard(uid)

    @staticmethod
    def _parse(text):
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

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

    def context(self, uid, recent_user_text):
        """The block that goes into the system prompt: who this person is, as far as the selves know."""
        with self._lock(uid):
            prof = self._load(uid)
        lv, days = self.level(prof)
        names = {"bange": "半格", "pingzuo": "平坐"}
        out = ["【你对他的了解】（两个“另一个他”共用；他说过的才算数）",
               "认识第 %d 天，他一共说过 %d 句话。" % (days, prof["msgs"])]
        if lv <= 1:
            out.append("你们还不太熟：多听，少下判断，别装作很懂他。")
        if prof["portrait"]:
            out.append("他是怎样的人：" + prof["portrait"])
        if prof["notes"]:
            out.append("记得的事（新的在前）：")
            out += ["- %s（%s）" % (x["text"], x["d"][5:]) for x in prof["notes"][:40]]
        eps = prof["episodes"]
        if eps:
            out.append("最近几次聊天：")
            out += ["- %s 跟%s：%s" % (e["d"][5:], names.get(e.get("p"), "你们"), e["text"]) for e in eps[-5:]]
            older = eps[:-5]
            q = _bigrams(recent_user_text)
            if older and q:
                scored = sorted(((len(q & _bigrams(e["text"])), i) for i, e in enumerate(older)), reverse=True)
                hits = [older[i] for s, i in scored[:3] if s >= 2]
                if hits:
                    out.append("可能跟今天有关的往事：")
                    out += ["- %s：%s" % (e["d"], e["text"]) for e in hits]
        return "\n".join(out)
