#!/usr/bin/env python3
"""MyLife phase-0 demo backend. Stdlib only; nginx serves the page, this process answers /api/*.

Secrets come from the environment (systemd EnvironmentFile=/etc/mylife/secrets.env):
  TENCENT_SECRET_ID, TENCENT_SECRET_KEY, TENCENT_TTS_REGION, TENCENT_TOKENHUB_API_KEY, DASHSCOPE_API_KEY

Endpoints
  GET  /api/health | /api/voices | /api/cards | /api/persona?p= | /api/self?u=
  POST /api/chat     {p, u, nick, profile, notme, history}   -> {reply, model, ms}
  POST /api/card     {p, u, nick, id}                         -> {text, model, ms}   彩虹卡开场
  POST /api/closing  {p, u, nick, history}                    -> {text, model, ms}   收尾一句
  POST /api/sync     {u}  fold the latest talk into long-term memory (server/memory.py)
  POST /api/self/forget {u, id} | /api/self/wipe {u} | /api/self/import {u, notes}
  POST /api/fb       {p, mid, v, reply, user}                 👍/👎, logged for distilling
  POST /api/tts      {v, cps, p, t}  -> audio/mpeg (Tencent TextToVoice, cached per chunk)
  POST /api/asr      16 kHz mono WAV -> {text}  (Tencent 一句话识别, audio not stored)
"""
import base64
import datetime
import functools
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memory import NEW_USER_LINE, Memory, valid_uid, write_json_atomic

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.environ.get("MYLIFE_VOICES", os.path.join(HERE, "voices.json"))
CACHE_DIR = os.environ.get("MYLIFE_CACHE", "/var/cache/mylife")
BIND = os.environ.get("MYLIFE_BIND", "127.0.0.1")
PORT = int(os.environ.get("MYLIFE_PORT", "18960"))
DAILY_CHAR_CAP = int(os.environ.get("MYLIFE_TTS_DAILY_CHARS", "100000"))
PER_IP_PER_MIN = int(os.environ.get("MYLIFE_TTS_PER_MIN", "40"))
MAX_TEXT = 600
CHUNK = 140  # Tencent TextToVoice accepts <= 150 Chinese characters per call

# Same definition as countChars() in the page: what counts as a spoken character.
SPOKEN = re.compile(r"[一-龥A-Za-z0-9]")
# Emoji are shown in the chat bubble but must not be read aloud.
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def count_chars(text):
    return len(SPOKEN.findall(text))


# ---------------------------------------------------------------- Tencent TTS
class TTSError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class TencentTTS:
    HOST = "tts.tencentcloudapi.com"
    SERVICE = "tts"
    VERSION = "2019-08-23"

    def __init__(self, secret_id, secret_key, region="ap-shanghai"):
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.region = region

    def _call(self, action, params, timeout=20):
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        ts = int(time.time())
        date = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        ctype = "application/json; charset=utf-8"
        canonical = "\n".join([
            "POST", "/", "",
            f"content-type:{ctype}\nhost:{self.HOST}\nx-tc-action:{action.lower()}\n",
            "content-type;host;x-tc-action",
            hashlib.sha256(body).hexdigest(),
        ])
        scope = f"{date}/{self.SERVICE}/tc3_request"
        to_sign = "\n".join([
            "TC3-HMAC-SHA256", str(ts), scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ])

        def _h(key, msg):
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        k = _h(_h(_h(("TC3" + self.secret_key).encode("utf-8"), date), self.SERVICE), "tc3_request")
        sig = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            f"https://{self.HOST}/", data=body, method="POST",
            headers={
                "Authorization": (f"TC3-HMAC-SHA256 Credential={self.secret_id}/{scope}, "
                                  f"SignedHeaders=content-type;host;x-tc-action, Signature={sig}"),
                "Content-Type": ctype,
                "Host": self.HOST,
                "X-TC-Action": action,
                "X-TC-Timestamp": str(ts),
                "X-TC-Version": self.VERSION,
                "X-TC-Region": self.region,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TTSError("HTTP%d" % e.code, e.read()[:300].decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TTSError("Network", str(e))
        resp = data.get("Response", {})
        if "Error" in resp:
            raise TTSError(resp["Error"].get("Code", "Unknown"), resp["Error"].get("Message", ""))
        return resp

    def synth(self, text, voice, speed=0.0, codec="mp3", rate=16000, emotion=None):
        params = {
            "Text": text,
            "SessionId": uuid.uuid4().hex,
            "VoiceType": int(voice),
            "Speed": float(speed),
            "Volume": 0,
            "Codec": codec,
            "SampleRate": int(rate),
            "PrimaryLanguage": 1,
            "ModelType": 1,
        }
        if emotion:
            params["EmotionCategory"] = emotion
        resp = self._call("TextToVoice", params)
        audio = resp.get("Audio")
        if not audio:
            raise TTSError("EmptyAudio", resp.get("RequestId", ""))
        return base64.b64decode(audio)


class TencentASR(TencentTTS):
    """一句话识别 SentenceRecognition: <= 60 s, <= 3 MB per call. Same TC3 signer, different product."""
    HOST = "asr.tencentcloudapi.com"
    SERVICE = "asr"
    VERSION = "2019-06-14"

    def recognize(self, wav):
        resp = self._call("SentenceRecognition", {
            "EngSerViceType": "16k_zh", "SourceType": 1, "VoiceFormat": "wav",
            "Data": base64.b64encode(wav).decode("ascii"), "DataLen": len(wav),
            "FilterModal": 1,      # drop most filler words (嗯、啊) so the text reads like typing
            "ConvertNumMode": 1,   # 三点 -> 3点 only where it reads naturally
        }, timeout=30)
        return (resp.get("Result") or "").strip(), int(resp.get("AudioDuration") or 0)


def wav_seconds(wav):
    """Duration of a 16-bit PCM WAV from its header; None if it is not one."""
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return None
    byte_rate = int.from_bytes(wav[28:32], "little")
    return (len(wav) - 44) / float(byte_rate) if byte_rate else None


# Tencent Speed parameter: documented anchor points (multiplier -> Speed).
SPEED_ANCHORS = [(0.6, -2.0), (0.8, -1.0), (1.0, 0.0), (1.2, 1.0), (1.5, 2.0), (2.5, 6.0)]


def speed_param(mult):
    mult = max(0.6, min(2.5, mult))
    for (m0, s0), (m1, s1) in zip(SPEED_ANCHORS, SPEED_ANCHORS[1:]):
        if mult <= m1:
            return round(s0 + (s1 - s0) * (mult - m0) / (m1 - m0), 2)
    return 6.0


def speed_for_cps(curve, cps):
    """curve: measured [[Speed, chars/sec], ...] ascending in Speed. Interpolate inside the
    measured range; outside it, scale from the nearest end using the documented anchors."""
    pts = sorted(curve)
    if cps <= pts[0][1] or cps >= pts[-1][1]:
        s_end, c_end = pts[0] if cps <= pts[0][1] else pts[-1]
        mult_end = dict((s, m) for m, s in SPEED_ANCHORS).get(s_end)
        if mult_end is None:  # end speed not an anchor: invert speed_param numerically
            mult_end = min((abs(speed_param(m / 100) - s_end), m / 100) for m in range(60, 251))[1]
        return max(-2.0, min(6.0, speed_param(mult_end * cps / c_end)))
    for (s0, c0), (s1, c1) in zip(pts, pts[1:]):
        if c0 <= cps <= c1:
            return round(s0 + (s1 - s0) * (cps - c0) / (c1 - c0), 2)
    return 0.0


def split_text(text, limit=CHUNK):
    """Split at sentence ends, then commas, then hard; pack pieces up to `limit`."""
    pieces = [p for p in re.split(r"(?<=[。！？!?；;…\n])", text) if p.strip()]
    out = []
    for p in pieces:
        while len(p) > limit:
            cut = max(p.rfind(c, 0, limit) for c in "，,、：: ")
            cut = cut + 1 if cut > limit // 3 else limit
            out.append(p[:cut])
            p = p[cut:]
        if p.strip():
            out.append(p)
    chunks, cur = [], ""
    for p in out:
        if cur and len(cur) + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur.strip():
        chunks.append(cur)
    return chunks


def strip_id3(data):
    if data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[10 + size:]
    return data


# ---------------------------------------------------------------- chat (LLM)
# Backend picks ONE chat model; users never choose. The fallback is only used when the
# primary call fails, so the conversation doesn't break.
LLM_BASES = {
    "tokenhub": ("https://tokenhub.tencentmaas.com/v1", "TENCENT_TOKENHUB_API_KEY"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
}
CHAT_PRIMARY = (os.environ.get("MYLIFE_CHAT_ENDPOINT", "tokenhub"),
                os.environ.get("MYLIFE_CHAT_MODEL", "deepseek-v4.1-flash"))
CHAT_FALLBACK = (os.environ.get("MYLIFE_CHAT_FALLBACK_ENDPOINT", "dashscope"),
                 os.environ.get("MYLIFE_CHAT_FALLBACK_MODEL", "qwen3.8-max"))
PERSONAS = {"bange": ("半格", "理性", "pingzuo"), "pingzuo": ("平坐", "陪伴", "bange")}
PERSONA_DIR = os.environ.get("MYLIFE_PERSONAS", os.path.join(HERE, "personas"))
PROMPT_DIR = os.path.join(HERE, "prompts")
STYLE_PATH = os.environ.get("MYLIFE_STYLE", os.path.join(PROMPT_DIR, "de-ai-style.txt"))
SELF_FRAME_PATH = os.environ.get("MYLIFE_SELF_FRAME", os.path.join(PROMPT_DIR, "self-frame.txt"))
CLOSING_PATH = os.environ.get("MYLIFE_CLOSING_STYLE", os.path.join(PROMPT_DIR, "closing-style.txt"))
CARDS_PATH = os.environ.get("MYLIFE_CARDS", os.path.join(PROMPT_DIR, "rainbow-cards.json"))
OUTPUT_RULE = "只输出你要说的话本身：纯文本，不加引号，不加名字前缀，不写动作或旁白。"
CHAT_PER_MIN = int(os.environ.get("MYLIFE_CHAT_PER_MIN", "20"))
CHAT_DAILY = int(os.environ.get("MYLIFE_CHAT_DAILY", "3000"))
ASR_PER_MIN = int(os.environ.get("MYLIFE_ASR_PER_MIN", "20"))
ASR_DAILY = int(os.environ.get("MYLIFE_ASR_DAILY", "3000"))
ASR_MAX_BYTES = 2_200_000  # 60 s of 16 kHz mono 16-bit is ~1.92 MB
FB_PER_MIN = 60
CHAT_LOG_DIR = os.path.join(CACHE_DIR, "chatlog")
FEEDBACK_DIR = os.path.join(CACHE_DIR, "feedback")
# Same signals as the page's R.crisis plus a few softer ones; the page answers the explicit ones with a
# fixed safety message before calling us, this is the net for whatever reaches the model.
CRISIS = re.compile(r"(不想活|自杀|想死|轻生|结束生命|活着(好|真|太|也)?没(什么)?意思|活着没有意义|活着有什么意义"
                    r"|不想醒来|消失了?也?(没有?|不会有)人|了结自己|割腕|安眠药|撑不下去|不想再撑)")
SAFETY_LINE = "如果你有伤害自己的念头，请马上联系身边信任的人，或拨打心理援助热线 12356；情况紧急请拨打 110 或 120。"


def postprocess(user_text, reply):
    reply = reply.strip().strip("“”\"")
    if CRISIS.search(user_text or "") and "12356" not in reply:
        reply += "\n\n" + SAFETY_LINE
    return reply


# ---------------------------------------------------------------- LLM
class LLMError(Exception):
    pass


class DailyCap(LLMError):
    def __init__(self):
        super().__init__("DailyCap")


def llm_call(endpoint, model, messages, max_tokens=300, temperature=0.8, timeout=25):
    base, keyvar = LLM_BASES[endpoint]
    key = os.environ.get(keyvar, "")
    if not key:
        raise LLMError("%s missing" % keyvar)
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False}
    if endpoint == "tokenhub" and model.startswith("deepseek"):
        body["thinking"] = {"type": "disabled"}  # thinking adds ~1s and is not needed for chat
    req = urllib.request.Request(base + "/chat/completions", method="POST",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError("HTTP%d %s" % (e.code, e.read()[:200].decode("utf-8", "replace")))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise LLMError(str(e))
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("bad response")
    return text.strip()


def complete(msgs, max_tokens=300, temperature=0.8, timeout=25, tidy=str.strip):
    """Primary model, then the fallback if it fails or says nothing. Returns (text, model)."""
    for endpoint, model in (CHAT_PRIMARY, CHAT_FALLBACK):
        try:
            text = tidy(llm_call(endpoint, model, msgs, max_tokens, temperature, timeout))
            if text:
                return text, model
            err = "empty reply"
        except LLMError as e:
            err = str(e)
        sys.stderr.write("%s failed (%s)\n" % (model, err))
    raise LLMError(err)


# ---------------------------------------------------------------- prompts
def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


@functools.lru_cache(maxsize=None)
def persona_prompt(pid):
    """人格档案 + 另一个你 + 风格示例（提示词部分）+ 去AI味风格块。读一次缓存；deploy.sh 每次都会重启服务。"""
    pdir = os.path.join(PERSONA_DIR, pid)
    # HTML comments hold editorial notes (blueprint names, what to avoid); keep them out of the prompt.
    parts = [re.sub(r"<!--.*?-->", "", _read(os.path.join(pdir, "人格档案.md")), flags=re.S).strip(),
             _read(SELF_FRAME_PATH).strip()]
    m = re.search(r"<!-- prompt-examples:start -->(.*?)<!-- prompt-examples:end -->",
                  _read(os.path.join(pdir, "风格示例.md")), re.S)
    if m:
        parts.append("【风格示例（只看说法和思路，不许照抄句子，按当下对话重新说）】\n" + m.group(1).strip())
    parts.append(_read(STYLE_PATH).strip())
    return "\n\n".join(p for p in parts if p)


@functools.lru_cache(maxsize=None)
def load_cards():
    try:
        with open(CARDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {"colors": data["colors"], "cards": data["cards"]}
    except (OSError, ValueError, KeyError):
        return {"colors": [], "cards": []}


def clip(s, n):
    return str(s or "").strip()[:n]


def system_prompt(pid, *blocks):
    """Every endpoint's system prompt: persona, then the blocks it opts into, then the output rule."""
    return "\n\n".join([persona_prompt(pid)] + [b for b in blocks if b] + [OUTPUT_RULE])


def user_block(req):
    return "【这位用户】\n称呼：%s" % (clip(req.get("nick"), 12) or "朋友")


def build_messages(pid, req, know=None):
    """Chat: persona + who the user is (+ what the selves know) + the last 16 turns."""
    name, _, other = PERSONAS[pid]
    oname, okind, _ = PERSONAS[other]
    ctx = [user_block(req)]
    prof = clip(req.get("profile"), 800)
    if prof:
        ctx.append("性格画像（只当线索，不当结论；不要逐条复述，也不要主动提星座、MBTI 这些词，除非对方先提）：\n" + prof)
    notme = clip(req.get("notme"), 200)
    if notme:
        ctx.append("用户标了“不像我”的画像条目（不要再拿这些说事）：" + notme)
    if know:
        ctx.append(know)
    ctx.append("【此刻】今天是 %s。用户正在和你（%s）聊。另一位是%s（%s）；当用户需要的东西%s更合适时，可以建议去找%s。"
               % (datetime.date.today().isoformat(), name, oname, okind, oname, oname))
    msgs = [{"role": "system", "content": system_prompt(pid, "\n".join(ctx))}]
    for h in (req.get("history") or [])[-16:]:
        role = "user" if h.get("role") == "user" else "assistant"
        content = clip(h.get("content"), 500)
        if content:
            if msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + content  # merge consecutive same-role turns
            else:
                msgs.append({"role": role, "content": content})
    if msgs[-1]["role"] != "user":
        raise ValueError("last message must be from the user")
    return msgs


CLOSING_TASK = ("【现在】用户要结束这次聊天了。用你（%s）的口吻，对用户说最后一句话，把今天聊的东西收住。"
                "硬性要求：只说一句，不超过 40 个字；必须落在用户今天亲口说过的具体内容上，别人套不上；"
                "不给建议，不提问，不用感叹号，不用表情，不说“今天聊了很多”“希望你”“加油”“一切都会好”这类话。")


def build_closing(pid, req):
    """收尾一句: persona + 收尾一句的写法 + task; the user message is the transcript. Returns (msgs, what the user said)."""
    lines, said = [], []
    for h in (req.get("history") or [])[-20:]:
        c = clip(h.get("content"), 300)
        if c and h.get("role") == "user":
            lines.append("用户：" + c)
            said.append(c)
        elif c:
            lines.append("你：" + c)
    if not said:
        raise ValueError("nothing to close")
    system = system_prompt(pid, _read(CLOSING_PATH).strip(), user_block(req), CLOSING_TASK % PERSONAS[pid][0])
    user = "这是这次的聊天记录：\n" + "\n".join(lines) + "\n\n用户要走了。说你的最后一句。"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], "\n".join(said)


def tidy_closing(text):
    """First line only, without quotes; cut long ones at a sentence break (the prompt asks for <= 40 字)."""
    text = text.strip().strip("“”\"「」")
    text = text.splitlines()[0].strip() if text else ""
    if len(text) > 60:
        cut = max(text.rfind("。", 0, 60), text.rfind("，", 0, 60))
        text = text[:cut + 1] if cut > 10 else text[:60]
    return text


CARD_TASK = ("【现在】他刚翻开今天的彩虹卡，上面写着「%s」（%s色，话题：%s）。这是今天聊天的开头。"
             "用你的口吻接住这张卡：一到两句，先用你自己的话轻轻回应它（不要复述卡上的原话），再问一个具体、好回答的小问题。"
             "可以联系你记得的他的事，但别硬凑。不评价这张卡（不说“来得巧”“正好”“这句话说得好”），不提也不解读卡的颜色，不讲运势、能量、脉轮，不说教。")


def build_card(pid, req, card, know=None):
    """彩虹卡开场: persona + who the user is + what the selves know + task."""
    color = next((c for c in load_cards()["colors"] if c["key"] == card["c"]), {"name": "", "theme": ""})
    system = system_prompt(pid, user_block(req), know or NEW_USER_LINE, CARD_TASK % (card["t"], color["name"], color["theme"]))
    return [{"role": "system", "content": system}, {"role": "user", "content": "（我抽到一张彩虹卡：「%s」）" % card["t"]}]


# ---------------------------------------------------------------- app state
class App:
    def __init__(self):
        sid = os.environ.get("TENCENT_SECRET_ID", "")
        skey = os.environ.get("TENCENT_SECRET_KEY", "")
        if not sid or not skey:
            sys.exit("TENCENT_SECRET_ID / TENCENT_SECRET_KEY missing from environment")
        region = os.environ.get("TENCENT_TTS_REGION", "ap-shanghai")
        self.tts = TencentTTS(sid, skey, region)
        self.asr = TencentASR(sid, skey, region)
        with open(CATALOG_PATH, encoding="utf-8") as f:
            self.catalog = json.load(f)
        self.voices = {str(v["id"]): v for v in self.catalog["voices"]}
        self.pool = ThreadPoolExecutor(max_workers=6)       # audio chunks, on the request path
        self.fold_pool = ThreadPoolExecutor(max_workers=2)  # memory folds, in the background
        self.mem = Memory(os.path.join(CACHE_DIR, "users"),
                          lambda msgs, max_tokens, temperature: self.llm(msgs, max_tokens, temperature, timeout=40)[0],
                          {pid: p[0] for pid, p in PERSONAS.items()})
        self.lock = threading.Lock()       # rate limits
        self.disk_lock = threading.Lock()  # usage.json and the jsonl logs
        self.hits = {k: defaultdict(deque) for k in ("tts", "chat", "asr", "fb")}
        for d in (CACHE_DIR, CHAT_LOG_DIR, FEEDBACK_DIR):
            os.makedirs(d, exist_ok=True)
        self.usage_path = os.path.join(CACHE_DIR, "usage.json")
        try:
            with open(self.usage_path) as f:
                self.usage = json.load(f)
        except (OSError, ValueError):
            self.usage = {}

    # -- limits
    def allow_ip(self, ip, kind):
        now = time.time()
        limit = {"tts": PER_IP_PER_MIN, "chat": CHAT_PER_MIN, "asr": ASR_PER_MIN, "fb": FB_PER_MIN}[kind]
        with self.lock:
            q = self.hits[kind][ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def _bump(self, key, n, cap):
        day = datetime.date.today().isoformat()
        with self.disk_lock:
            if self.usage.get("day") != day:
                self.usage = {"day": day}
            used = self.usage.get(key, 0)
            if used + n > cap:
                return False
            self.usage[key] = used + n
            write_json_atomic(self.usage_path, self.usage)
            return True

    # -- LLM endpoints: build the prompt, call, tidy, remember, log
    def llm(self, msgs, max_tokens=300, temperature=0.8, timeout=25, tidy=str.strip):
        """Every model call goes through here: daily cap, primary then fallback, timing. -> (text, model, ms)"""
        if not self._bump("chat_calls", 1, CHAT_DAILY):
            raise DailyCap()
        t0 = time.time()
        text, model = complete(msgs, max_tokens, temperature, timeout, tidy)
        return text, model, int((time.time() - t0) * 1000)

    def chat(self, pid, req, ip):
        uid = req.get("u") if valid_uid(req.get("u")) else None
        users = [clip(h.get("content"), 300) for h in (req.get("history") or [])[-6:] if h.get("role") == "user"]
        msgs = build_messages(pid, req, self.mem.context(uid, " ".join(users[-3:])) if uid else None)
        reply, model, ms = self.llm(msgs)
        reply = postprocess(msgs[-1]["content"], reply)
        self.log_turn("chat", ip, pid, req, msgs[-1]["content"], reply, model, ms)
        if uid and self.mem.append(uid, pid, [("user", users[-1] if users else msgs[-1]["content"]), ("ai", reply)]) >= 8:
            self.fold_pool.submit(self.fold, uid, 8)
        return {"reply": reply, "model": model, "ms": ms}

    def closing(self, pid, req, ip):
        """One sentence that sums up the session, said when the user taps 结束本次对话."""
        msgs, said = build_closing(pid, req)
        text, model, ms = self.llm(msgs, max_tokens=120, temperature=0.7, tidy=tidy_closing)
        text = postprocess(said, text)
        self.log_turn("closing", ip, pid, req, said[-300:], text, model, ms)
        if valid_uid(req.get("u")):
            self.mem.append(req["u"], pid, [("closing", text)])
            self.fold_pool.submit(self.fold, req["u"], 2)
        return {"text": text, "model": model, "ms": ms}

    def card(self, pid, req, ip):
        """Opening line for a 彩虹卡 the user just drew, in the persona's voice and with its memory."""
        card = next((c for c in load_cards()["cards"] if c["id"] == str(req.get("id", ""))), None)
        if not card:
            raise ValueError("unknown card")
        uid = req.get("u") if valid_uid(req.get("u")) else None
        text, model, ms = self.llm(build_card(pid, req, card, self.mem.context(uid, card["t"]) if uid else None),
                                   max_tokens=160)
        text = postprocess("", text)
        self.log_turn("card", ip, pid, req, card["t"], text, model, ms)
        if uid:
            self.mem.append(uid, pid, [("card", "（抽到彩虹卡「%s」）" % card["t"]), ("ai", text)])
        return {"text": text, "model": model, "ms": ms}

    def fold(self, uid, min_user):
        try:
            return self.mem.consolidate(uid, min_user)
        except Exception as e:  # background work must never take the server down
            sys.stderr.write("consolidate %s failed: %s\n" % (uid[:6], e))
            return None

    # -- logs
    def log_turn(self, kind, ip, pid, req, user, reply, model, ms):
        self._append(CHAT_LOG_DIR, {"kind": kind, "ip": ip, "p": pid, "nick": clip(req.get("nick"), 12),
                                    "user": user, "reply": reply, "model": model, "ms": ms})

    def log_feedback(self, rec):
        """👍/👎 on replies: the upvoted ones feed the next round of distilling (tools/upvoted.py)."""
        self._append(FEEDBACK_DIR, rec)

    def _append(self, folder, rec):
        rec = {"t": datetime.datetime.now().isoformat(timespec="seconds"), **rec}
        path = os.path.join(folder, datetime.date.today().isoformat() + ".jsonl")
        with self.disk_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # -- synthesis with per-chunk disk cache
    def chunk_audio(self, text, voice, speed, rate, emotion):
        key = hashlib.sha256(f"{voice}|{speed}|{rate}|{emotion or ''}|{text}".encode("utf-8")).hexdigest()
        path = os.path.join(CACHE_DIR, key[:2], key + ".mp3")
        try:
            with open(path, "rb") as f:
                return f.read(), True
        except OSError:
            pass
        if not self._bump("tts_chars", len(text), DAILY_CHAR_CAP):
            raise TTSError("DailyCap", "今日朗读字数已达上限")
        data = self.tts.synth(text, voice, speed, "mp3", rate, emotion)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".%d.tmp" % threading.get_ident()
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return data, False

    def speak(self, text, voice_id, cps, persona):
        v = self.voices[voice_id]
        if v.get("curve"):
            speed = speed_for_cps(v["curve"], cps)
        else:
            speed = speed_param(cps / float(v.get("baseCps") or 4.0))
        emotion = (v.get("emotion") or {}).get(persona)
        rate = int(v.get("rate", 16000))
        futs = [self.pool.submit(self.chunk_audio, c, voice_id, speed, rate, emotion) for c in split_text(text)]
        parts, cached = [], True
        for i, fu in enumerate(futs):
            data, hit = fu.result()
            cached = cached and hit
            parts.append(data if i == 0 else strip_id3(data))
        return b"".join(parts), speed, cached

    def public_voices(self):
        keep = ("id", "name", "gender", "desc", "tier", "baseCps")
        return {"provider": "tencent",
                "defaults": self.catalog.get("defaults", {}),
                "voices": [{k: v[k] for k in keep if k in v} for v in self.catalog["voices"]]}


APP = None


class Handler(BaseHTTPRequestHandler):
    server_version = "mylife/0.1"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_ip(), fmt % args))

    def client_ip(self):
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            raise ValueError("bad body size")
        req = json.loads(self.rfile.read(n).decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("bad body")
        return req

    def limited(self, kind):
        """True (and a 429 already sent) when this IP is over its per-minute limit."""
        if APP.allow_ip(self.client_ip(), kind):
            return False
        self.send_json(429, {"error": "too many requests"})
        return True

    def llm_failed(self, e):
        sys.stderr.write("llm error %s\n" % e)
        return self.send_json(429 if isinstance(e, DailyCap) else 502, {"error": str(e)[:120]})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if path == "/api/voices":
            return self.send_json(200, APP.public_voices())
        if path == "/api/cards":
            return self.send_json(200, load_cards())
        if path == "/api/self":
            if not valid_uid(q.get("u")):
                return self.send_json(400, {"error": "bad user id"})
            if not self.limited("fb"):
                self.send_json(200, APP.mem.summary(q["u"]))
            return None
        if path == "/api/persona":
            pid = q.get("p", "")
            if pid not in PERSONAS:
                return self.send_json(400, {"error": "unknown persona"})
            return self.send_json(200, {"p": pid, "model": CHAT_PRIMARY[1],
                                        "text": _read(os.path.join(PERSONA_DIR, pid, "人格档案.md"))})
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        handler = {"/api/chat": self.handle_llm, "/api/closing": self.handle_llm, "/api/card": self.handle_llm,
                   "/api/sync": self.handle_self, "/api/self/forget": self.handle_self,
                   "/api/self/wipe": self.handle_self, "/api/self/import": self.handle_self,
                   "/api/fb": self.handle_fb, "/api/asr": self.handle_asr, "/api/tts": self.handle_tts}.get(path)
        if not handler:
            return self.send_json(404, {"error": "not found"})
        return handler(path)

    def handle_llm(self, path):
        """/api/chat, /api/closing, /api/card: {p, u, nick, ...} -> the persona's words."""
        try:
            req = self.read_json(48000)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if self.limited("chat"):
            return None
        pid = str(req.get("p", ""))
        if pid not in PERSONAS:
            return self.send_json(400, {"error": "unknown persona"})
        run = {"/api/chat": APP.chat, "/api/closing": APP.closing, "/api/card": APP.card}[path]
        try:
            return self.send_json(200, run(pid, req, self.client_ip()))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except LLMError as e:
            return self.llm_failed(e)

    def handle_self(self, path):
        """Long-term memory: /api/sync folds the latest talk in (called when the user leaves a chat);
        forget / wipe let the user delete what the selves know; import seeds from older local notes."""
        try:
            req = self.read_json(8192)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        uid = req.get("u")
        if not valid_uid(uid):
            return self.send_json(400, {"error": "bad user id"})
        if self.limited("chat" if path == "/api/sync" else "fb"):
            return None
        if path == "/api/sync":
            r = APP.fold(uid, 2) or {"add": [], "episode": ""}
            return self.send_json(200, {"memories": r["add"], "episode": r["episode"]})
        if path == "/api/self/forget":
            return self.send_json(200, {"ok": APP.mem.forget(uid, str(req.get("id", "")))})
        if path == "/api/self/wipe":
            APP.mem.wipe(uid)
            return self.send_json(200, {"ok": True})
        notes = req.get("notes") if isinstance(req.get("notes"), list) else []
        return self.send_json(200, {"imported": APP.mem.import_notes(uid, notes)})

    def handle_fb(self, path):
        """Body: {p, mid, v: 1|-1|0, reply, user}. Logged for distilling upvoted replies."""
        try:
            req = self.read_json(8192)
            pid, v = str(req.get("p", "")), int(req.get("v", 0))
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if pid not in PERSONAS or v not in (-1, 0, 1):
            return self.send_json(400, {"error": "bad request"})
        if self.limited("fb"):
            return None
        APP.log_feedback({"ip": self.client_ip(), "p": pid, "mid": clip(req.get("mid"), 40), "v": v,
                          "reply": clip(req.get("reply"), 600), "user": clip(req.get("user"), 300)})
        return self.send_json(200, {"ok": True})

    def handle_tts(self, path):
        """Body: {v, cps, p, t} -> audio/mpeg."""
        try:
            req = self.read_json(16384)
            voice = str(req.get("v", ""))
            text = EMOJI.sub("", str(req.get("t", ""))).strip()
            persona = str(req.get("p", ""))
            cps = float(req.get("cps", 5.6))
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if voice not in APP.voices:
            return self.send_json(400, {"error": "unknown voice"})
        if not count_chars(text):
            return self.send_json(400, {"error": "empty text"})
        if len(text) > MAX_TEXT:
            return self.send_json(413, {"error": "text too long"})
        if not (2.0 <= cps <= 10.0):
            return self.send_json(400, {"error": "cps out of range"})
        if self.limited("tts"):
            return None
        t0 = time.time()
        try:
            audio, speed, cached = APP.speak(text, voice, cps, persona)
        except TTSError as e:
            sys.stderr.write("tts error voice=%s %s\n" % (voice, e))
            code = 429 if e.code in ("DailyCap", "LimitExceeded.AccessLimit") else 502
            return self.send_json(code, {"error": e.code, "message": e.message})
        self.send_audio(audio, speed, cached, int((time.time() - t0) * 1000))

    def handle_asr(self, path):
        """Body: a 16 kHz mono 16-bit WAV recorded in the page. The audio is not stored."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 44 or n > ASR_MAX_BYTES:
            return self.send_json(413, {"error": "bad audio size"})
        wav = self.rfile.read(n)
        secs = wav_seconds(wav)
        if secs is None:
            return self.send_json(400, {"error": "not a wav"})
        if secs < 0.3 or secs > 61:
            return self.send_json(400, {"error": "bad duration"})
        if self.limited("asr"):
            return None
        if not APP._bump("asr_calls", 1, ASR_DAILY):
            return self.send_json(429, {"error": "DailyCap"})
        t0 = time.time()
        try:
            text, _ = APP.asr.recognize(wav)
        except TTSError as e:
            sys.stderr.write("asr error %s\n" % e)
            return self.send_json(502, {"error": e.code})
        return self.send_json(200, {"text": text, "secs": round(secs, 1), "ms": int((time.time() - t0) * 1000)})

    def send_audio(self, audio, speed, cached, ms):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-TTS-Speed", str(speed))
        self.send_header("X-TTS-Cache", "hit" if cached else "miss")
        self.send_header("X-TTS-Ms", str(ms))
        self.end_headers()
        self.wfile.write(audio)


def main():
    global APP
    APP = App()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("mylife listening on %s:%d, %d voices\n" % (BIND, PORT, len(APP.voices)))
    srv.serve_forever()


if __name__ == "__main__":
    main()
