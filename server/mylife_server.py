#!/usr/bin/env python3
"""MyLife phase-0 demo backend. Stdlib only; nginx serves the page, this process answers /api/*.

Secrets come from the environment (systemd EnvironmentFile=/etc/mylife/secrets.env):
  TENCENT_SECRET_ID, TENCENT_SECRET_KEY, TENCENT_TTS_REGION, TENCENT_TOKENHUB_API_KEY, DASHSCOPE_API_KEY, ARK_API_KEY (optional)

Endpoints
  GET  /api/health | /api/voices | /api/cards | /api/persona?p= | /api/self?u= | /api/records?u=&kind=diet|sleep|mood
  POST /api/chat     {p, u, nick, profile, notme, birth, history} -> {reply, model, ms, tid}
  POST /api/chart    {date, time, slot, city}                 -> 排盘（八字、紫微、星盘，server/paipan.py）：{items, animal, text, ...}
  POST /api/card     {p, u, id}                               -> {ok}   今天抽到的彩虹卡记进长期记忆（卡由页面念，不接话）
  POST /api/open     {p, u, nick, follow}                     -> {text, model, ms, tid, follow}   进对话时先开口（两个人格记忆共享）；
                     follow = 首页红点那件事的 id（他点了带红点的卡）：开口就说那件事，回来的 follow 是用上的那件
  POST /api/closing  {p, u, nick, history}                    -> {text, model, ms, tid}   收尾一句
  POST /api/portrait {u, sign, mbti, blood, animal}          -> {sign:[{h,t}], mbti:[{h,t}], mix, heard}  性格画像的展开
  POST /api/sync     {u}  fold the latest talk into long-term memory (server/memory.py); GET /api/self 的 due = 首页红点 {id, p}
  POST /api/self/forget {u, id} | /api/self/wipe {u} | /api/self/import {u, notes}
  POST /api/records/delete {u, kind, id} | /api/records/clear {u, kind}   我的记录：饮食、睡眠、心情（memory.py）
  POST /api/fb       {p, u, tid, mid, v, reply, user}         👍/👎 on one reply
  POST /api/event    {p, u, kind: local|rating, ...}          what the page did on its own: scripted replies, session ratings
  POST /api/tts      {v, cps, p, t}  -> audio/mpeg (Tencent TextToVoice, cached per chunk)
  POST /api/asr?u=&p= 16 kHz mono WAV -> {text}  (Tencent 一句话识别, audio not stored)

Everything said goes to one archive, $MYLIFE_DATA/logs/<day>.jsonl, one record per line with a `kind`: chat, open,
closing, card, fb, local, rating, asr, error. Chat records carry the exact model input (history + memory block + prompt
version), so tools/chatlog.py can replay real conversations against new prompts. Long-term memory: $MYLIFE_DATA/users.
"""
import base64
import datetime
import functools
import hashlib
import hmac
import itertools
import json
import os
import random
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

import paipan
from memory import (BUSY, NEW_USER_LINE, RECORD_DAYS, RECORD_KINDS, RECORD_NAMES, Memory, meal_line, parse_json, parse_kcal, valid_uid,
                    write_json_atomic)

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.environ.get("MYLIFE_VOICES", os.path.join(HERE, "voices.json"))
CACHE_DIR = os.environ.get("MYLIFE_CACHE", "/var/cache/mylife")  # TTS audio, daily counters: safe to delete
DATA_DIR = os.environ.get("MYLIFE_DATA", "/var/lib/mylife")      # memory and the conversation archive: keep
LOG_DIR = os.path.join(DATA_DIR, "logs")
BIND = os.environ.get("MYLIFE_BIND", "127.0.0.1")
PORT = int(os.environ.get("MYLIFE_PORT", "18960"))
DAILY_CHAR_CAP = int(os.environ.get("MYLIFE_TTS_DAILY_CHARS", "100000"))
PER_IP_PER_MIN = int(os.environ.get("MYLIFE_TTS_PER_MIN", "40"))
MAX_TEXT = 1800  # the longest chat reply (an answer to a practical question, PRACTICAL_CAP) must still be readable aloud
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
    "ark": ("https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY"),  # 火山方舟: Doubao Seed (doubao-seed-2-1-lite-260915 ...)
}
CHAT_PRIMARY = (os.environ.get("MYLIFE_CHAT_ENDPOINT", "tokenhub"),
                os.environ.get("MYLIFE_CHAT_MODEL", "deepseek-v4.1-flash"))
CHAT_FALLBACK = (os.environ.get("MYLIFE_CHAT_FALLBACK_ENDPOINT", "dashscope"),
                 os.environ.get("MYLIFE_CHAT_FALLBACK_MODEL", "qwen3.8-max"))
BASE_NAMES = {"bange": "半格", "pingzuo": "平坐"}
# Display names (user 2026-11-22: 导师 / 伙伴; earlier that day the test code names 黄执中 / 大麦哥). The prompt files keep
# 半格/平坐 and system_prompt() swaps these in. MYLIFE_TEST_ALIAS=0 falls back to the placeholder names.
ALIAS = {"bange": "导师", "pingzuo": "伙伴"} if os.environ.get("MYLIFE_TEST_ALIAS", "1") != "0" else {}
PERSONAS = {"bange": (ALIAS.get("bange", "半格"), "理性", "pingzuo"), "pingzuo": (ALIAS.get("pingzuo", "平坐"), "陪伴", "bange")}
ALIAS_NOTE = ("【名字】你在界面上叫“%s”。名字只是个称呼，你还是他的另一个自己。%s"
              "你也没有自己的生活：爱好、运动、工作、婚姻、家人、身体，一样都没有；"
              "被问到（比如骑不骑车、离没离婚、以前做什么），就说你没有自己的日子，知道的都是对方告诉你的。")
# What each name must not turn into (a title invites the matching stance).
NAME_NOTE = {"导师": "叫“导师”不等于比他高一辈：不说教，不讲大道理，不布置作业，不叫他“同学”“孩子”，不说“作为你的导师”；"
                     "你跟他站在同一边，只是多看半步。",
             "伙伴": "“伙伴”就是陪他一起走的那个，不端着，不说“作为你的伙伴”。"}
PERSONA_DIR = os.environ.get("MYLIFE_PERSONAS", os.path.join(HERE, "personas"))
PROMPT_DIR = os.path.join(HERE, "prompts")
STYLE_PATH = os.environ.get("MYLIFE_STYLE", os.path.join(PROMPT_DIR, "de-ai-style.txt"))
SELF_FRAME_PATH = os.environ.get("MYLIFE_SELF_FRAME", os.path.join(PROMPT_DIR, "self-frame.txt"))
CLOSING_PATH = os.environ.get("MYLIFE_CLOSING_STYLE", os.path.join(PROMPT_DIR, "closing-style.txt"))
CARDS_PATH = os.environ.get("MYLIFE_CARDS", os.path.join(PROMPT_DIR, "rainbow-cards.json"))
PORTRAIT_PATH = os.environ.get("MYLIFE_PORTRAIT", os.path.join(PROMPT_DIR, "portrait.txt"))
PORTRAIT_HEARD_PATH = os.path.join(os.path.dirname(PORTRAIT_PATH), "portrait-heard.txt")
# 接着上次的事（docs/方案_接着上次的事与红点留言.md）: things he said are coming up get a red dot on that self's card
# when they should have an outcome, and one line about it when he taps it. MYLIFE_FOLLOW=0 turns it all off.
FOLLOW_ON = os.environ.get("MYLIFE_FOLLOW", "1") != "0"
SYNC_WAIT = 20  # s /api/sync waits for his turn being answered and a running fold (the page gives it 40 and asks again on busy)
# Pictures he sends (client 2026-11-24: "支持上传图片，这样可以上传系统排盘截图或者其他图片"). A model first writes down
# what is in the picture (a 排盘 screenshot copied out palace by palace) and the page sends that text as his message, so
# the history, the memory and the logs stay text. On a test 紫微 screenshot deepseek-v4.1-flash copied all twelve palaces
# right three times out of three in ~4 s; qwen3.7-plus took 17-26 s and misread a star now and then, so it is the fallback.
SEE_PATH = os.path.join(os.path.dirname(PORTRAIT_PATH), "see-image.txt")
SEE_MODELS = ((os.environ.get("MYLIFE_SEE_ENDPOINT", "tokenhub"), os.environ.get("MYLIFE_SEE_MODEL", "deepseek-v4.1-flash")),
              ("dashscope", "qwen3.7-plus"))
IMAGE_MAX_BYTES = 3_000_000  # the page sends a JPEG of at most 2400 px a side, usually 300-800 KB
IMAGE_DAILY = int(os.environ.get("MYLIFE_IMAGE_DAILY", "500"))
IMAGE_PER_MIN = 20  # up to 6 pictures in one message, and he may swap a few before sending
IMAGE_DIR = os.path.join(DATA_DIR, "images")  # kept with the logs while testing, to check a misread chart
IMAGE_URL = re.compile(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=\s]+)")
SIGNS = {"白羊": "火", "狮子": "火", "射手": "火", "金牛": "土", "处女": "土", "摩羯": "土",
         "双子": "风", "天秤": "风", "水瓶": "风", "巨蟹": "水", "天蝎": "水", "双鱼": "水"}
MBTI_TYPES = {a + b + c + d for a in "EI" for b in "SN" for c in "TF" for d in "JP"}
BLOOD_TYPES = ("A", "B", "O", "AB")
ANIMALS = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
OUTPUT_RULE = ("只输出你要说的话本身：纯文本，不加引号，不加名字前缀，不写动作或旁白，"
               "不写括号里的备注、自言自语或写给自己的提示；对他说话用“你”，不在回复里用“他”指他。"
               "说到他的事，只用他这次聊天里说过的和“你对他的了解”里写着的；“上次”“你说过”“你每次”这类话，"
               "只在那里有记录时才说。没有的细节不编，拿不准就说“我猜”，或者不说。")
HIST_CLIP = {"user": 2000, "assistant": 1000}  # per turn, in 字 (was 500: a long story got cut mid-sentence)
CHAT_MAX_TOKENS = 1600  # room for REPLY_MAX 字; everyday replies stay short anyway
REPLY_MAX = 1100  # hard cap on a chat reply in 字 (memory.DEPTH lowers it while the selves barely know him)
CHAT_PER_MIN = int(os.environ.get("MYLIFE_CHAT_PER_MIN", "100"))  # per IP; testers often share one office Wi-Fi
CHAT_DAILY = int(os.environ.get("MYLIFE_CHAT_DAILY", "3000"))
ASR_PER_MIN = int(os.environ.get("MYLIFE_ASR_PER_MIN", "20"))
ASR_DAILY = int(os.environ.get("MYLIFE_ASR_DAILY", "3000"))
ASR_MAX_BYTES = 2_200_000  # 60 s of 16 kHz mono 16-bit is ~1.92 MB
FB_PER_MIN = 60
# "用大麦哥的口吻开导我": the selves already talk in their own way, so the model never sees the name (it would only
# start explaining that it won't imitate anyone). Only asks aimed at the self, or naming a public figure, are cut.
STYLE_ASK = re.compile(r"(?:请你?|麻烦你?|你(?:能不能|可以|就)?|能不能)?(?:用|按照?|学|模仿|以)([^，。！？,.!?\s]{1,20}?)的?"
                       r"(?:口吻|风格|语气|腔调|说话方式|方式)(?:来)?(?:回复|回答|回)?(?:我)?(?:一下)?")
STYLE_WHO = re.compile(r"黄执中|大麦哥|博主|网红|名人|明星|主播|老师|抖音|小红书|B站|知乎|奇葩说")
# A healing app, not a crisis line (client 2026-11-22: "危机处理做的太重了…没有人想听到让打热线"). Only an explicit
# wish to die counts here; the page uses the same rule (crisisSaid). Stress and hyperbole ("撑不住了", "累得想死",
# "想死你了", "不想活成我妈那样") are ordinary talk; "活着没意思" / "想消失" the selves hold in their own words (persona
# 【安全】) without hotlines or "are you safe?".
CRISIS = re.compile(r"自杀|轻生|结束(自己的)?生命|了结自己|割腕|(?<![得到])想死(?![你您他她]|我了)|(?<![得到])不想活(?![成在得])")
SAFETY_LINE = "要是这个念头一直压着你，先别一个人扛，找个信得过的人陪你待一会儿。想找人说说的话，12356 也有人接。"
# Sentences that treat him as at risk; dropped unless he said something explicit recently.
AT_RISK = re.compile(r"12356|心理援助|热线|伤害自己|轻生|自杀|不想活|现在安全吗|安不安全")
# Client: no time words — the selves don't know when he is reading. The prompt forbids them; this catches the stock
# "今晚先…" / "今天就…" / "明天再说" that still slip through, unless he used the word himself.
TIME_ADV = re.compile(r"(今晚|今天|今夜)(?=先|就|才|没|要是|能|别|不用|可以|只|也|还|不)")


def untime(reply, recent):
    if "明天" not in recent:
        reply = reply.replace("明天的事明天再说", "后面的事回头再说").replace("明天再说", "回头再说")
    return TIME_ADV.sub(lambda m: m.group(0) if m.group(1) in recent else "", reply)


def drop_sentences(reply, pattern):
    """The reply without the sentences that match `pattern` (kept whole if nothing would be left)."""
    paras = [re.sub(r"[^。！？\n]*(?:%s)[^。！？\n]*[。！？]?" % pattern, "", p).strip() for p in reply.split("\n")]
    return "\n\n".join(p for p in paras if p) or reply


# The selves don't know each other exists (user 2026-11-23, again 2026-11-24 on old logs: "你跟黄执中说的那件事",
# "伙伴更合适"). The prompts never name the other one; this is the net under them: a sentence naming the other self, or
# the people they are styled on (the test code names before 2026-11-22), never reaches him. A name he used himself
# (his own 导师, 合作伙伴) is his word, not the other self.
REAL_NAMES = "黄执中|大麦哥"
OTHER_NAMES = {"bange": ("平坐", r"(?<![合作小好老])伙伴"), "pingzuo": ("半格", "导师")}


def drop_other_self(pid, reply, said=""):
    names = [REAL_NAMES] + [n for n in OTHER_NAMES.get(pid, ()) if not re.search(n, said or "")]
    return drop_sentences(reply, "|".join(names)) if re.search("|".join(names), reply) else reply


COPY_LIMIT = 0.5  # share of a reply's 4-character pieces already in one earlier reply
REWRITE_NOTE = "【重写】你刚写的回复跟你前面说过的一条几乎一样。他不需要再看一遍同样的话：换个角度、换个说法重新回他，前面说过的不再说。"
REPEAT_NOTE = ("他把前面说过的一句又原样发了一遍：可能是觉得你上次没说到点上，或者想听你换个说法。"
               "别照着你上次的回答再说一遍，换个角度、换个说法重新回他。")


def grams4(text):
    t = re.sub(r"\s", "", text or "")
    return {t[i:i + 4] for i in range(len(t) - 3)}


def overlap(text, earlier):
    """How much of `text` is already in `earlier` (0..1), by 4-character pieces."""
    a = grams4(text)
    return len(a & grams4(earlier)) / len(a) if a else 0.0


def same_line(a, b):
    norm = lambda s: re.sub(r"[\s，。！？、,.!?…~～]", "", str(s or ""))
    return len(norm(a)) >= 6 and norm(a) == norm(b)


HARD_END = re.compile(r"我(?:就|一直)?在(?:这儿|这里|呢)|我在[。！]|我陪(?:着)?你|陪你(?:待|坐)|想哭就哭|喝[口点杯]?热|早点睡|洗把脸")


# An answer to a practical question that ends by handing the choice back ("这个客户值不值得……你比我清楚", "怎么定是你的").
HANDBACK = re.compile(r"你比我清楚|只有你(?:自己)?(?:最)?(?:清楚|知道|能定)|怎么定[^。！？\n]{0,6}(?:是你的|你说了算)|轮不到我"
                      r"|你(?:自己)?心里(?:其实)?(?:早就|已经)?有(?:答案|数|杆秤)|那杆秤|你(?:心里)?其实已经知道")


def drop_stock_end(reply, rx=HARD_END):
    """A long chat reply doesn't end on a stock line (我陪你, 想哭就哭, 喝口热的): told not to in the prompt, the selves
    still did (real user 2026-11-24: "回答还是比较公式化"). Short replies ("嗯，我在。") keep theirs."""
    paras = [p.strip() for p in reply.split("\n") if p.strip()]
    if len(paras) < 2:
        return reply
    keep = "".join(x for x in re.findall(r"[^。！？]+[。！？]?", paras[-1]) if not rx.search(x)).strip()
    if len(keep) == len(paras[-1]):
        return reply
    return "\n\n".join(paras[:-1] + ([keep] if keep else []))


def postprocess(user_text, reply, recent=None, told=False, pid=None):
    """recent = his last few lines (chat only). When none of them is an explicit wish to die, sentences that bring up the
    hotline or self-harm are dropped. When his line is one, one soft line is added, once per chat (told = said before).
    With recent, stock time words he didn't use are taken out (see untime). With pid, sentences naming the other self go."""
    if recent is not None:
        reply = untime(reply, recent)
    if pid:
        reply = drop_other_self(pid, reply, (user_text or "") + (recent or ""))
    reply = drop_asides(strip_md(reply))
    if len(reply) > 1 and reply[0] in "“\"" and reply[-1] in "”\"" and not re.search("[“”\"]", reply[1:-1]):
        reply = reply[1:-1].strip()  # the model wrapped the whole reply in quotes; a quote that opens a sentence stays
    reply = re.sub(r'"([^"\n]{1,60})"', r"“\1”", reply)  # 中文里的成对半角引号换成全角
    if pid and recent is not None:
        reply = drop_stock_end(reply)
    if CRISIS.search(user_text or ""):
        if not told and "12356" not in reply:
            reply += "\n\n" + SAFETY_LINE
    elif recent is not None and not CRISIS.search(recent) and AT_RISK.search(reply):
        reply = drop_sentences(reply, AT_RISK.pattern)
    return reply


def looping(text, times=4):
    """A vision model stuck repeating one line (temperature 0 on a table of bowls: the same bowl seventy times)."""
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    return any(lines.count(l) >= times for l in set(lines))


def unloop(text):
    """Keep the first two of any repeated line and say the rest was dropped."""
    out, seen, cut = [], {}, False
    for l in (text or "").split("\n"):
        k = l.strip()
        seen[k] = seen.get(k, 0) + 1
        if k and seen[k] > 2:
            cut = True
            continue
        out.append(l)
    return "\n".join(out).rstrip() + ("\n（有一处重复看了很多遍，可能没看清，数量按常理算）" if cut else "")


def strip_md(text):
    """The page shows plain text: long chart reads came back with **星盘** headings and "- " bullets shown as is."""
    text = re.sub(r"\*\*|__|^#{1,6}[ \t]*", "", text, flags=re.M)
    return re.sub(r"^[ \t]*[-*•][ \t]+", "", text, flags=re.M).strip()


ASIDE = re.compile(r"[（(][^（）()\n]*[）)]")


def drop_asides(reply):
    """Bracketed notes the model writes to itself (a whole paragraph in brackets, or one opening the reply) never reach him."""
    paras = [p for p in reply.split("\n") if not ASIDE.fullmatch(p.strip())]
    out = "\n".join(paras).strip()
    m = ASIDE.match(out)
    if m and "他" in m.group(0):
        out = out[m.end():].strip()
    return re.sub(r"\n{3,}", "\n\n", out) or reply


# ---------------------------------------------------------------- LLM
class LLMError(Exception):
    pass


class DailyCap(LLMError):
    def __init__(self):
        super().__init__("DailyCap")


def llm_call(endpoint, model, messages, max_tokens=300, temperature=0.8, timeout=25, think=None):
    base, keyvar = LLM_BASES[endpoint]
    key = os.environ.get(keyvar, "")
    if not key:
        raise LLMError("%s missing" % keyvar)
    body = {"model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": False}
    if endpoint == "ark" or endpoint == "tokenhub" and model.startswith("deepseek"):
        body["thinking"] = {"type": "disabled"}  # thinking adds ~1s and is not needed for chat (Seed 2.x thinks by default too)
    if endpoint == "dashscope" and think is not None:
        body["enable_thinking"] = think  # qwen3.6+ think by default: 20-60 s to read a screenshot instead of a few
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


@functools.lru_cache(maxsize=None)
def view_library(pid):
    """personas/<pid>/观点库.json: topic cards distilled from the style reference's public talks and the comments under
    them (research in personas/pingzuo/调研/02), plus what ordinary people said to comfort each other under similar
    podcasts and songs (eases / feels, 调研/03). Paraphrased; no names, life facts or quotes. No file = no library."""
    try:
        with open(os.path.join(PERSONA_DIR, pid, "观点库.json"), encoding="utf-8") as f:
            return {t["id"]: t for t in json.load(f)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


ROUTE_PROMPT = ("下面是一些话题（括号里是这类话常见的说法）。读他最近说的话，挑出和他正在说的这件事真正贴得上的话题，最多两个，"
                "只输出编号，用逗号隔开。宁少勿多：只是沾到一个词、其实不是一回事的不选；都贴不上，就只输出：无\n\n%s")


# 星座、血型、紫微、八字、占星 (client 2026-11-24: "当用户提出要聊这些的时候，就专心聊这些"). The selves used to turn
# these down ("这个我看不了") and steer him back to his feelings ("你问这个，多半不是真想知道……").
_SIGN = "白羊|金牛|双子|巨蟹|狮子|处女|天秤|天平|天蝎|射手|人马|摩羯|水瓶|双鱼"
ASTRO = re.compile(r"星座|星盘|水逆|合盘|占星|宫位|十二宫|血型|(?:AB|[ABO])型|紫微|紫薇|斗数|命宫|主星|八字|日主|四柱|命盘|排盘|命理"
                   r"|五行|算命|本命年|太岁|生肖|属相|属(?:鼠|牛|虎|兔|龙|蛇|马|羊|猴|鸡|狗|猪)"
                   r"|白羊|金牛|巨蟹|天秤|天蝎|摩羯|水瓶|双鱼|(?:%s)座"  # 狮子、处女、双子、射手 alone mean other things
                   r"|(?:太阳|月亮|上升|金星|火星|水星)(?:星座|在|落|是)?(?:%s)" % (_SIGN, _SIGN)
                   + r"|(?:月亮|上升)(?:和|跟|与|、)?(?:上升|月亮|太阳)|(?:月亮|上升)(?:星座)?(?:是|在)(?:什么|啥|哪)"
                   r"|运势|大运|流年|身宫|四化|化[禄权科忌]|十神|喜用|用神|纳音|日元"
                   r"|第?[一二三四五六七八九十]{1,2}宫(?![殿廷])|在(?:几|哪个?|哪一)宫"
                   # follow-ups a tester sent mid-chart (2026-11-24) that went undetected, so the chart and the no-prediction
                   # rule were missing and the self made up pillars and a health forecast
                   r"|转运|走运|运气|本命年|出生时间|生辰|几点(?:出)?生|出生(?:在|于|是)?(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\d"
                   r"|(?:19|20)?\d{2}年(?:会|能|怎|运|好不好|顺)"
                   # "按我的盘分析一下未来两年的感情" (2026-11-24 test) went undetected, so 导师 had no chart and asked for the city
                   # not stock talk: "看盘到半夜", "大盘上涨", "这个盘口"
                   r"|(?:我的|我这个?|这个?|按|照|排|看(?:看|一下))盘(?![子点口中整])|(?:命|星|我的|你的|这张)盘(?:上|面)"
                   r"|时间节点|重要的?时间点"
                   r"|(?:未来|接下来|今后|往后)(?:一|两|三|几|五)?年的?(?:感情|姻缘|桃花|事业|财运|运势|运气|走势|走向)")


CHART_PARTS = {
    "bazi": re.compile(r"八字|日主|日元|五行|四柱|十神|喜用|用神|纳音|天干|地支|生肖|属相|本命年|太岁|大运|流年"
                       r"|属(?:鼠|牛|虎|兔|龙|蛇|马|羊|猴|鸡|狗|猪)"),
    "ziwei": re.compile(r"紫微|紫薇|斗数|命宫|身宫|主星|四化|化[禄权科忌]"),
    "west": re.compile(r"星座|星盘|占星|上升|月亮|太阳|[水金火木土]星|水逆|合盘|宫位|第?[一二三四五六七八九十]{1,2}宫|在(?:几|哪个?|哪一)宫"
                       r"|" + _SIGN)}


def chart_parts(lines):
    """Which of his charts his last line asks about; all three when it names none (运势, 转运, a birth time, 27年会怎样):
    given only the planets, the self worked out the 八字 itself and got the pillars wrong. A bare follow-up ("准吗",
    "还有呢") stays on the chart the line before it named. lines = his lines in this astrology run, oldest first."""
    for i, line in enumerate(reversed(lines)):
        got = [k for k, p in CHART_PARTS.items() if p.search(line)]
        if got or i == 0 and is_astro(line):
            return got or list(CHART_PARTS)
    return list(CHART_PARTS)


# Stock endings (user 2026-11-24 "结尾的固定套路要优化一下": 伙伴 ended 54% of replies with 我在这儿 / 想哭就哭 /
# 喝口热的, 导师 kept coming back to 先别急着…… / 怎么定，是你的; after one such ending the next reply usually repeated it).
END_TICS = re.compile(r"我(?:就|一直)?在(?:这儿|这里|呢)|我在。|我陪(?:着)?你|陪你(?:待|坐)|想哭就哭|喝[口点杯]?热|热水|热乎|洗把脸"
                      r"|睡一觉|早点睡|缓一缓|歇会儿|别(?:跟|和)自己较劲|先别(?:急着|逼自己)|怎么定[^。！？\n]{0,6}(?:是你的|说了算)|一件小事|眼下(?:能做|先|就)"
                      r"|只有一件|不用(?:马上)?逼自己|难受就难受")


# How a follow-up reply goes, picked at random so consecutive replies don't share one shape. Length still follows
# what he said (de-ai-style rule 1): a new layer of the story still gets said through.
FOLLOW_SHAPES = {
    "pingzuo": ["这一条就抓他刚才那句里的一个词或一个画面，往里说", "这一条说说你自己听到这儿心里是什么滋味，再说一句只对他成立的话",
                "这一条短一点，两三句，像坐在旁边随口接一句", "这一条跟他一起看那件事里的一个细节，不急着安慰"],
    "bange": ["这一条只给一个判断，把理由说清楚", "这一条换个角度，从那件事的另一头、或者那个人那边看一眼",
              "这一条短一点，一两句点破就行", "这一条把他这句里最要紧的那个词拿出来，说准它"],
}

# Devices that turn into a template when every reply uses them (real replies 2026-11-24: "不是……是……" in 83%).
TICS = {"不是……是……": re.compile(r"不是[^。！？\n]{0,24}?[，,]?(?:而)?是"), "我猜": re.compile(r"我猜"),
        "换谁都……": re.compile(r"换(?:谁|成谁|了谁|个人)"), "说真的": re.compile(r"说真的|说实话"),
        "先别急着……": re.compile(r"先别急着"), "不用逼自己……": re.compile(r"不用(?:马上)?逼自己|别逼自己"),
        "……很正常": re.compile(r"(?:太|很|再)正常(?:不过)?了"), "不丢人": re.compile(r"不丢人")}


def used_endings(hist):
    """Stock phrases this self already ended a reply with in this visit (most recent first)."""
    seen = []
    for h in reversed(hist):
        paras = [p for p in str(h.get("content") or "").split("\n") if p.strip()] if h.get("role") != "user" else []
        for m in END_TICS.finditer(paras[-1] if paras else ""):
            if m.group() not in seen:
                seen.append(m.group())
    return seen[:4]


def birth_chart(birth):
    """The page's birth fields {date, time, slot, city, geo?} -> paipan.chart (cached), or None when there is no usable
    date. geo = [name, lat, lon, tz] for a city the table doesn't have (see Geo)."""
    if not isinstance(birth, dict) or not birth.get("date"):
        return None
    geo = birth.get("geo")
    geo = tuple(geo) if isinstance(geo, (list, tuple)) and len(geo) == 4 else None
    try:
        return paipan.chart(str(birth["date"])[:10], clip(birth.get("time"), 5), clip(birth.get("slot"), 4),
                            clip(birth.get("city"), 40), geo)
    except (ValueError, TypeError):
        return None


GEO_PROMPT = ("下面是一个人填的出生地：“%s”。给出这个地方（城市、县或区）的大致经纬度和时区。"
              "只输出一个 JSON：{\"name\": \"规范的地名\", \"country\": \"所在国家或地区\", \"lat\": 纬度, "
              "\"lon\": 经度（东经为正）, \"tz\": \"IANA 时区名\"}。认不出来、或者不是一个地名，就只输出 {}。")
# Mainland births are recorded in Beijing time, Xinjiang's too. A lat/lon box used to decide that and also caught Korea,
# Kyushu, northern Vietnam, Nepal, northern India and the Russian Far East.
CHINA_TZ = {"Asia/Shanghai", "Asia/Urumqi", "Asia/Chongqing", "Asia/Chungking", "Asia/Harbin", "Asia/Kashgar", "PRC"}


class Geo:
    """Birth places the city table doesn't have (县级市、县、海外小城): one short model call each, kept in
    $MYLIFE_DATA/geo.json so a place is looked up once. Mainland China always gets Beijing time."""
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path, encoding="utf-8") as f:
                self.known = json.load(f)
        except (OSError, ValueError):
            self.known = {}

    def find(self, city, ask):
        city = clip(city, 40)
        known = paipan.place(city) if city else None
        if not city or known and not known["approx"]:  # "浙江苍南" only matched its province: look the county up
            return None
        with self.lock:
            if city in self.known:
                return self.known[city] or None
        try:
            got = parse_json(ask([{"role": "user", "content": GEO_PROMPT % city}])) or {}
        except LLMError:
            return None  # a failed call is not remembered: try again next time
        try:
            lat, lon, tz = float(got["lat"]), float(got["lon"]), str(got.get("tz") or "")
            where = city + str(got.get("name") or "") + str(got.get("country") or "")
            if (tz in CHINA_TZ or str(got.get("country") or "") in ("中国", "中国大陆")) \
                    and not re.search(r"香港|澳门|台湾|台北|高雄|台中", where):
                tz = "Asia/Shanghai"
            paipan.ZoneInfo(tz)
            found = [clip(got.get("name"), 20) or city, round(lat, 2), round(lon, 2), tz] if -90 < lat < 90 and -180 < lon <= 180 else []
        except (KeyError, TypeError, ValueError):  # not a place (or not one the model knows): remember that too
            found = []
        with self.lock:
            self.known[city] = found
            write_json_atomic(self.path, self.known)
        return found or None


ASTRO_NOTE = ("他在聊星座、血型、紫微、八字、占星、MBTI、性格分析这一类：专心陪他聊这个，照【他想聊星座、八字这些】来，"
              "不猜他为什么问，不把话拉回他的心事，也不扯这次前面聊过的别的事、以前说过的事（他自己连上的除外）。"
              "他让你分析、剖析，就照【深入分析】说透；随口一问，几句就行。"
              "他在聊天里说的出生时间、出生地，以【他的盘】为准；【他的盘】里没有的，不自己推算、不编干支和星位。"
              "缺出生时间排不了的（紫微、时柱、上升和宫位），就请他在这里直接说几点几分出生，说了程序就排上，不用他去改资料，"
              "也不用他去别的工具、App 排（这里自己能排）；他手上有别处排好的盘，可以发截图来。"
              "八字看感情、排大运要分男女：他没说过自己是男是女，就别自己定（不说“男命”“女命”），用得着就问一句。")
# 以后会怎样 (client 2026-11-24): 导师 reads the chart objectively and gives 时间节点 from 【流年和行运】; 伙伴 stays as it was,
# talking about temperament and tendencies, never which year brings what. Health, disaster, lifespan and lawsuits stay out
# for both (a tester's family illness, see PREDICT).
FUTURE_NOTE = {
    "bange": ("看盘你是客观分析：不安慰、不铺垫、不讲道理，排出来什么说什么。先摆盘面，再一条条下判断，每条说清是盘上哪一处看出来的；"
              "几样说的一致还是打架，直说。说到以后，照【流年和行运】给时间节点：哪一年、哪几个月，盘上哪方面有动静"
              "（感情、事业学业、钱、搬动出行、人际），通常会以什么样子出现，该留意什么；他问以后几年，表里的每一年都讲到，"
              "每年至少一个节点，木星土星那几段能到月份的给到月份；他要具体到哪年哪月，就照表给，不含糊。"
              "说成盘上的说法（“按流年……”“木星走到……这段”），讲到倾向和时机为止，不打包票说一定发生；【流年和行运】里没有的年份和事不编。"
              "健康、病灾、寿命、官司、家人安危不断（疾厄宫、化忌、煞星照性情和做事讲，不往身体和病上说，也不用声明你不说）：他问，就说盘上讲的是性情和时机，断不了这些。"
              "长的分析可以分几块说（比如盘面、性格、感情、事业、时间节点），每块开头一行短标题，不加符号。"
              "他问以后的时候，整条回复在一千四百字以内：盘面压到两三句，篇幅留给每一年的节点，宁可每年说短一点，也别讲不到后面的年份。"),
    "pingzuo": ("不预言：不说哪一年会发生什么、会不会转运，不说他或家人的健康、病情、灾祸、寿命、婚姻、官司会怎样；"
                "他追着要“确切一点”“别含糊”，也不给，直说这些说法讲的是性情和倾向，讲不了哪年发生什么，再接着聊他关心的那件事本身。"
                "他问某一年（本命年、太岁、流年）：讲老讲究里对这一年的说法，句句落在“说法是”“老讲究里讲”上，"
                "不说“会发生”“真的会动”“会转运”这种断定，也不把说法套到他的钱、工作、家人身上去断事。")}

SAVED_NOTE = ("他刚在聊天里说了%s：程序已经按这个排进【他的盘】，也替他存进了资料。直接照【他的盘】说，"
              "不用让他再改资料、再发一次；你前面自己推的时辰、时柱、命宫跟盘上不一样的，就直说前面说得不对，以盘为准。")
BIRTH_FIELD = {"date": "出生日期 ", "time": "出生时间 ", "city": "出生地 "}
# What the self said before the chart had his birth time (a tester, 2026-11-24): with "紫微排不了" and "时柱乙酉" in its own
# earlier replies, 导师 repeated both after the time was in, although 【他的盘】 had the 紫微 palaces and 时柱甲申; told in
# 【此刻】 that those were wrong, it still went with its history and once made up a 命宫. So those sentences are also taken
# out of its earlier replies before they go to the model.
STALE_ZIWEI = r"紫[微薇][^。！？\n]{0,24}(?:排不了|排不出|没法排|排不全|不能排)|十四主星|另一套算法|排盘的?工具"
PILLAR_SAID = r"时柱(?:是|就是|为|排出来是)?([甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥])"
MING_SAID = r"(?<!流年)命宫(?:在|落在|是)([子丑寅卯辰巳午未申酉戌亥])"


def stale_claims(chart, hist):
    """-> (a line for 【此刻】, hist with the self's out-of-date chart claims taken out)."""
    said = "\n".join(str(h.get("content") or "") for h in hist if h.get("role") != "user")
    out, drop = [], []
    if chart.get("ziwei"):
        if re.search(STALE_ZIWEI, said):
            out.append("你前面说过紫微排不了：那时还没有他的出生时间，现在【他的盘】里有紫微了，照着说，先直说一句现在排上了")
            drop.append(STALE_ZIWEI)
        wrong = {m for m in re.findall(MING_SAID, said) if m != chart["ziwei"]["palaces"][0]["branch"]}
        if wrong:
            drop.append(r"(?<!流年)命宫(?:在|落在|是)[%s]" % "".join(wrong))
    hour = chart["bazi"]["pillars"][3] if len(chart["bazi"]["pillars"]) == 4 else ""
    wrong = sorted({m for m in re.findall(PILLAR_SAID, said) if m != hour})
    if hour and wrong:
        out.append("你前面说时柱是%s，【他的盘】上是%s：照盘说，提一句前面说的不对（多半是按钟表时间算的）" % ("、".join(wrong), hour))
        drop.append(r"时柱(?:是|就是|为|排出来是)?(?:%s)" % "|".join(wrong))
    if drop:
        hist = [h if h.get("role") == "user" else dict(h, content=drop_sentences(str(h.get("content") or ""), "|".join(drop)))
                for h in hist]
    return ("；".join(out) + "。" if out else ""), hist


IMAGE_MARK = "【图片】"  # the page's message for a picture: the mark, what the vision model saw, then his words if any
WORDS_MARK = "【他说】"  # his words after his pictures, in the one message the model gets (build_messages)
# A picture the vision model couldn't read: the page still sends it (review 2026-11-25: with it left out, "这些一共多少卡"
# was answered from the other pictures as if they were the whole meal).
IMAGE_LOST = IMAGE_MARK + "（这张图没看成，不知道上面是什么）"
IMAGE_LOST_NOTE = ("有的“【图片】”后面写着“这张图没看成”：那张你没看到，别猜上面是什么。他要你拿这几张图算总数、做比较、对一对，"
                   "就按看到的那几张算，并且明说少了一张没算进去，让他再发一次那张（或者说说上面是什么）再补上。")
IMAGE_NOTE = ("他发来图片：每个“【图片】”后面是看图写下的一张图的内容（他发的是图，不是这段字：就当你自己看到了这些图，"
              "不说“你发的文字”“描述里”“截图上写着”）；“【他说】”后面才是他打的字（以前的图，字在图的内容后面另起一行）。照他说的来：他要你拿图办事"
              "（算热量、认东西、翻译、对一对、看盘），就照图把事办了。只发了图、一句话没说的，就照图上的东西自然地接一两句，"
              "像朋友看到他发来的照片那样；不从图里推他的心思和处境，不编图外的事（几个人吃、为谁摆的、他这会儿什么状态）。"
              "图上是别处排的盘：就照图上的盘说；和【他的盘】有出入的地方（时辰、命宫、上升这些），点一句多半是出生时间或者算法"
              "（钟表时间还是真太阳时）不同，不争谁对。")


ASTRO_MORE = re.compile(r"呢|吗|继续|还有|然后|再说|多说|展开|细说|具体|确切|含糊|详细|准|像|哈|怎样|怎么样")


# 性格分析和 MBTI (user 2026-11-24: "对星座和mbti的分析停留在浅尝辄止……给了生日没法自动排盘，只能主动给它具体类别再分析"):
# "分析一下我的性格" never reached the chart, so he had to spell out 太阳狮子、月亮双鱼、上升摩羯 himself.
MBTI_TYPE = re.compile(r"(?<![A-Za-z])([EIei][NSns][TFtf][JPjp])(?![A-Za-z])")
ANALYZE = re.compile(r"(?:分析|剖析|解读|解析|说说|讲讲|看看|聊聊)(?:一下)?(?:我的?|我这个人的?)?(?:性格|特质|个性|人格|为人)"
                     # asked of the self, not said in despair ("我真不知道我是个什么样的人了，好累")
                     r"|(?:性格|特质|个性)(?:分析|特点|是什么|怎么样|如何)"
                     r"|(?:分析|剖析|解读|看看|说说|讲讲|猜猜|你觉得|你看)(?:一下)?我是(?:个|一个)?(?:什么样|怎样|啥样)的人")
TYPING = re.compile(ANALYZE.pattern + r"|(?i:mbti)|16\s*型(?:人格)?|(?<![A-Za-z])[iIeE]人|" + MBTI_TYPE.pattern)
DEEP = re.compile(ANALYZE.pattern + r"|分析|剖析|解读|解析|详细|深入|全面|细说|展开说|深一点|具体讲|说透|多讲讲")


def is_astro(text):
    return bool(ASTRO.search(text) or TYPING.search(text))


def astro_turn(users):
    """He is on astrology, MBTI or a personality read: his last line says so, or it is a short follow-up ("那双鱼呢",
    "准吗", "能说的确切一些吗？别那么含糊") to one that did ("我妈住院了" after one is not)."""
    return bool(users) and (is_astro(users[-1]) or (len(users) > 1 and len(users[-1]) <= 20 and
                                                    bool(ASTRO_MORE.search(users[-1])) and is_astro(users[-2])))


def deep_read(users):
    """He wants his chart / type read in depth (分析、剖析、详细……), not a passing remark."""
    return astro_turn(users) and bool(DEEP.search(users[-1]))


# Cognitive-function stacks (dominant, auxiliary, tertiary, inferior): the usual MBTI reading, so a type is more than its
# one-line label when he wants it explored.
FUNC = {"Ni": "内倾直觉：往深处看一件事的走向和本质，常常说不清理由就先“知道”了", "Ne": "外倾直觉：往外发散可能性，一个点能联想出一串",
        "Si": "内倾感觉：拿过去的经验和身体的感受做参照，重细节、重惯例", "Se": "外倾感觉：活在眼前的现场和感官里，反应快、上手快",
        "Ti": "内倾思考：在心里搭一套自洽的道理，要“说得通”", "Te": "外倾思考：按外面的标准和效率把事情组织起来，看结果",
        "Fi": "内倾情感：以自己心里认的价值为准，在乎真不真", "Fe": "外倾情感：感知和照顾身边人的情绪、场上的气氛"}
STACK = {"INFJ": "Ni Fe Ti Se", "INTJ": "Ni Te Fi Se", "ENFJ": "Fe Ni Se Ti", "ENTJ": "Te Ni Se Fi",
         "INFP": "Fi Ne Si Te", "INTP": "Ti Ne Si Fe", "ENFP": "Ne Fi Te Si", "ENTP": "Ne Ti Fe Si",
         "ISFJ": "Si Fe Ti Ne", "ISTJ": "Si Te Fi Ne", "ESFJ": "Fe Si Ne Ti", "ESTJ": "Te Si Ne Fi",
         "ISFP": "Fi Se Ni Te", "ISTP": "Ti Se Ni Fe", "ESFP": "Se Fi Te Ni", "ESTP": "Se Ti Fe Ni"}
GRIP = {"Se": "压力大时容易突然沉进感官刺激（吃、刷、买），或者对眼前的小事格外烦躁", "Ne": "压力大时脑子里冒出一串最坏的可能，越想越像真的",
        "Si": "压力大时揪着身体的不舒服和过去的失败不放，变得钻细节", "Ni": "压力大时突然觉得一切没意义，对以后有说不清的不祥预感",
        "Ti": "压力大时变得挑刺、冷冰冰，给自己和别人挑逻辑毛病", "Te": "压力大时突然强硬苛刻，非要立刻把事理清，骂自己没效率",
        "Fi": "压力大时情绪一下涌上来，觉得没人懂自己", "Fe": "压力大时突然特别在意别人怎么看自己，觉得被讨厌了"}


def mbti_block(code):
    s = STACK.get((code or "").upper())
    if not s:
        return ""
    f = s.split()
    return ("【他的 MBTI】%s（他自己填的或自己说的；MBTI 是一种性格说法，不是诊断）。按通行的认知功能说法，四个功能从强到弱：%s。"
            "劣势功能是 %s：%s。" % (code.upper(), "；".join("%s %s（%s）" % (r, x, FUNC[x]) for r, x in
                                                     zip(("主导", "辅助", "第三", "劣势"), f)), f[3], GRIP[f[3]]))


ANALYZE_NOTE = ("【深入分析】他要你分析他的性格（星座、八字、紫微、MBTI 这些）：这是要深聊，不是点到为止。"
                "先把他手上有的几样说清（盘上排出来的、他自己说的；他自己报的跟盘上不一样或盘上没有，以他说的为准），然后往深里走："
                "每一样不只说它代表什么，要说它在他身上大概怎么运转：什么场景会冒出来，背后在护着什么，代价是什么；"
                "把几样放在一起看，哪几样互相加强，哪几样在拉扯（相位、元素多寡、八字强弱、MBTI 的主导和劣势功能都可以拿来说），拉扯在日常里是什么样子；"
                "落到具体：跟人相处和亲密关系里、做事和工作里、压力大的时候、容易卡住的盲点、怎么用好它。"
                "每个说法都要能指出来自哪一样（“这是月亮双鱼那一面”“这是 Ni 在起作用”），不说空的形容词，不说谁都套得上的话。"
                "可以长，分七八段，八九百字到一千字，用你自己的说话方式说，不列点。")


# Forecasts about health and fate that the astrology talk must never give (2026-11-24 a tester, pressed for "确切一些", was
# told when a family member's illness would ease). The prompt says so; this drops any such sentence that still slips through.
# Chart talk turning 疾厄宫 / 化忌 into the body ("廉贞化忌在疾厄，是身体和情绪容易被拖", 导师 2026-11-24 test) even when told not to.
BODY = (r"身体[^。！？\n]{0,12}(?:被拖|拖累|消耗|透支|出问题|吃不消|不好|要注意|多注意|容易累|扛不住)"
        r"|疾厄[^。！？\n]{0,24}(?:身体|健康|病)")
PREDICT = re.compile(r"(?:病|病情|身体|健康|住院|手术|寿命|灾|血光|车祸|官司|破财|离婚)[^。！？\n]{0,24}"
                     r"(?:好转|缓和|稳下来|好起来|加重|恶化|出事|应验|躲不过|化解|有救|没救)")

# Birth details he says in chat. A tester (2026-11-24) answered "把出生时间给我" with "大概是下午五点四十左右", "就五点四十"
# and "17.40": none was recognised (only digits next to 出生 were), so the chart never got its 时辰, 紫微 stayed "排不了"
# and the selves sent her to edit her profile or bring a chart from another app, which she can't send here. A time counts
# when the line says it is a birth time (出生、生的、时辰……) or is nothing but a time, given right after a question about
# it; it wins over the profile's (he is correcting it) and the page saves it. A date or city only fills a blank.
_CN = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_NUM = r"\d{1,2}|[零〇一二两三四五六七八九十]{1,3}"
_PART = r"凌晨|半夜|早上|早晨|清晨|上午|中午|下午|傍晚|晚上|夜里"
CLOCK = re.compile(r"(%s)?\s*(?<![\d.:：])(%s)\s*(?:[:：]\s*(\d{2})(?!\d)|(\.)(\d{2})(?![\d.])|[点點时時]\s*(?:(半|一刻|三刻)|(%s)\s*分?)?)"
                   % (_PART, _NUM, _NUM))
# A time is his birth time when a birth word sits next to it ("五点四十出生的", "出生时间是17:40") or 生 follows it directly
# ("五点四十生的"). A bare 生的 / 点生 anywhere in the line also matched 陌生的、学生的、发生的、有点生气、差一点生病.
BIRTH_WORD = re.compile(r"出生|生于|生辰|时辰|时柱|几点生")
BORN_AFTER = re.compile(r"\s*(?:左右|前后|多|那会儿?|的时候)?\s*生(?:的|于)?(?![气病意活命人])")
MY_BIRTHDAY = re.compile(r"(?:^|我的?|本人的?)\s*生日")  # not 我妈生日
ASKED_TIME = re.compile(r"出生的?(?:具体|准确)?时间|几点几分|时辰|时柱|几点(?:出)?生|出生.{0,8}几点")
TIME_FILLER = re.compile(r"大概|大约|差不多|应该是?|可能|估计|好像|据说|就是?|是|在|我|的|了|左右|前后|那会儿?|时候|出生|生"
                         r"|准确|具体|时间|呢|吧|啊|呀|哦|嗯|哈|[，,。.!！~～\s]")
BIRTH_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})(?!\d)")
BIRTH_CITY = re.compile(r"(?:出生地|出生城市|出生在|出生于|(?<![发产诞衍])生[在于]|出生的?[，,]\s*(?=在))\s*(?:是|在)?\s*([一-鿿]{2,12})")


def _int(s):
    """'17' 17, '五' 5, '十七' 17, '四十五' 45, '零五' 5; None for anything else."""
    try:
        if s.isdigit():
            return int(s)
        a, ten, b = s.partition("十")
        return (_CN[a] if a else 1) * 10 + (_CN[b] if b else 0) if ten else int("".join(str(_CN[c]) for c in s))
    except (KeyError, ValueError):
        return None


def _clause(line, a, b):
    """The clause of `line` around line[a:b] ("我是1990年出生的，下午三点要开会": the 下午三点 clause has no 出生)."""
    stops = "，。！？,!?；;\n"
    start = max(line.rfind(c, 0, a) for c in stops) + 1
    ends = [i for i in (line.find(c, b) for c in stops) if i >= 0]
    return line[start:min(ends) if ends else len(line)]


def birth_in(line, asked, slot=""):
    """{time, date, city} stated in one line of his; asked = the self's line just before it."""
    out, said = {}, BIRTH_WORD.search(line)
    for m in CLOCK.finditer(line):
        part, h, mm, dot, dm, half, mi = m.groups()
        h, mi = _int(h), {"半": 30, "一刻": 15, "三刻": 45}[half] if half else _int(mm or dm or mi or "0")
        bare = not TIME_FILLER.sub("", line.replace(m.group(), "", 1)) and ASKED_TIME.search(asked)
        near = BORN_AFTER.match(line, m.end()) or BIRTH_WORD.search(_clause(line, m.start(), m.end()))
        if h is None or mi is None or not (bare or near and (not dot or h >= 13)):  # "8.12出生" is a date
            continue
        if part in ("下午", "傍晚", "晚上") and h < 12 or part in ("夜里", "半夜") and 6 <= h < 12 or part == "中午" and h < 6:
            h += 12
        elif part in ("凌晨", "晚上", "夜里", "半夜") and h == 12:
            h = 0
        elif not part and h < 12 and paipan.SLOTS.get(slot, (0,))[0] >= 12:  # "五点四十" from someone who picked 下午
            h += 12
        if h < 24 and mi < 60:
            out["time"] = "%02d:%02d" % (h, mi)
            break
    m = BIRTH_DATE.search(line) if said or MY_BIRTHDAY.search(line) else None
    if m:
        try:
            out["date"] = datetime.date(*map(int, m.groups())).isoformat()
        except ValueError:
            pass
    m = BIRTH_CITY.search(line)
    city = re.split(_PART + r"|的|人|出生", m.group(1))[0] if m else ""
    if len(city) >= 2:
        out["city"] = city
    return out


def said_birth(hist, slot="", time=""):
    """What he said in this history about his birth (later lines win), and what his last line said ({} if nothing).
    slot / time = the profile's; a time without 上午/下午 ("记错了，是五点二十出生的") takes the half of the day of the
    time he gave before."""
    found, prev, got = {}, "", {}
    for h in hist:
        said = str(h.get("content") or "")
        if h.get("role") == "user":  # a picture's birth data may be someone else's chart: not his
            half = slot or ("下午" if (found.get("time") or time or "00") >= "12" else "")
            got = {} if said.startswith(IMAGE_MARK) else birth_in(said, prev, half)
            found.update(got)
        else:
            prev, got = said, {}
    return found, got


def astro_now(hist):
    """astro_turn, plus a line that gives his birth time ("17.40" after "几点几分出生") so the chart comes with it (a city
    alone doesn't: "我生在农村，从小……" is his story, not a chart question)."""
    users = [str(h.get("content") or "") for h in hist if h.get("role") == "user"]
    return astro_turn(users[-2:]) or "time" in said_birth(hist)[1]


def astro_run(hist):
    """The tail of the history that is about star signs and the like (short lines such as "准吗" "嗯" go with it). Even
    told not to, the selves read whatever he said just before into his chart ("回到家喂完猫……这就是月亮双鱼")."""
    start = last = max((i for i, h in enumerate(hist) if h.get("role") == "user"), default=len(hist))
    for i in range(last, -1, -1):  # his last line always stays (a follow-up astro_turn accepted may not match below)
        said = str(hist[i].get("content") or "")
        if hist[i].get("role") == "user":
            if i < last and not (is_astro(said) or len(said) <= 12 or len(said) <= 20 and ASTRO_MORE.search(said)):
                break
            start = i
    return hist[start:]


def pick_views(pid, users, ask):
    """Up to two topic ids for what he is talking about (a short model call: ask(msgs) -> text). Skipped for one-liners
    like "嗯" or "好累", when the persona has no library, and when he is on astrology (the cards are about feelings and
    pulled him back to them)."""
    lib = view_library(pid)
    if not lib or not users or len(users[-1]) < 6 or astro_turn(users):
        return []
    topics = "\n".join("%s %s（%s）" % (i, t["topic"], "、".join(t["triggers"][:5])) for i, t in lib.items())
    try:
        text = ask([{"role": "system", "content": ROUTE_PROMPT % topics}, {"role": "user", "content": "\n".join(users[-3:])}])
    except LLMError:
        return []
    return [i for i in dict.fromkeys(re.findall(r"t\d{2}", text or "")) if i in lib][:2]


# 路子 (user 2026-11-24: "导师有套路没问题，但是套路不能单一……至少5种套路，每次回复根据具体情况选择一种"): 导师 used to walk
# one fixed path in every long reply (安慰 → 一般会说…可… → 重新定义 → 放回生活 → 断言). personas/<pid>/路子.json lists
# several whole routes; a short model call picks the one that fits what he just said, avoiding the last two used.
@functools.lru_cache(maxsize=4)
def moves(pid):
    try:
        with open(os.path.join(PERSONA_DIR, pid, "路子.json"), encoding="utf-8") as f:
            return {m["id"]: m for m in json.load(f)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


MOVE_PROMPT = ("你在帮一个聊天人格挑这一条回复的路子。下面是几种路子和各自适合的情况：\n%s\n\n"
               "读他最近说的话（最后一句最要紧）和这个人格上一条说了什么，挑最合适的一种。%s只输出编号，比如 m3。")


def pick_move(pid, users, last_ai, avoid, ask):
    """One route id for this reply, or None (no routes for this persona, a one-word line, astrology, a failed call)."""
    lib = moves(pid)
    if not lib or not users or len(users[-1].strip()) < 2 or astro_turn(users):
        return None
    menu = "\n".join("%s %s：%s" % (i, m["name"], m["when"]) for i, m in lib.items())
    skip = "这个人格最近两条用过 %s，这次换一种。" % "、".join(avoid) if avoid else ""
    if len(last_ai) >= 150 and "m8" in lib and "m8" not in avoid:  # a full route again on every follow-up read as a template
        skip += "它上一条已经说了一大段：他这句要是没讲出新的一层（新的事、新的人、新的担心），就选 m8。"
    said = "\n".join("他：" + u for u in users[-3:]) + ("\n这个人格上一条：" + clip(last_ai, 300) if last_ai else "")
    try:
        text = ask([{"role": "system", "content": MOVE_PROMPT % (menu, skip)}, {"role": "user", "content": said}])
    except LLMError:
        text = ""
    ids = [i for i in re.findall(r"m\d+", text or "") if i in lib and i not in avoid]
    return ids[0] if ids else random.choice([i for i in lib if i not in avoid] or list(lib))


# 实事 (client 2026-11-25, comparing one work-decision question across four apps — should I meet someone before a
# meeting: 导师 gave two roads, their costs and "你自己心里那杆秤……其实已经知道了"; 元宝、豆包、Grok gave a verdict, why,
# what to talk about and when not to go). The selves were built for heavy hearts: 导师 never decided for him and handed the
# choice back (路子 m6), 伙伴 said "这个我也拿不准". A practical matter he lays out and asks about now gets an answer:
# a verdict, the reasons from his situation, the next steps, and when the other choice is better (persona
# 【他问一件实际的事】). A cheap pattern first; a short model call tells a practical ask from a heavy heart ("好难受怎么办").
PRACTICAL_HINT = re.compile(r"该不该|要不要|需不需要|用不用|值不值|划不划算|[去见接借买投换签卖选]不[去见接借买投换签卖选]|还是不|选哪|哪个好|哪个更"
                            r"|需要[^。！？\n]{0,30}吗|怎么办|怎么(?:做|弄|搞|谈|说|讲|写|准备|安排|选|处理|应对|开口|回|跟|跟进|定价|报价)"
                            r"|如何|帮我(?:分析|看看|想想|出个?主意|参谋|捋|理|算)|分析一下|出个?主意|建议|方案|计划|策略|可行吗|行不行|合适吗"
                            r"|有没有必要|(?:聊|说|谈|带|准备|问|注意)(?:些|点)?什么"
                            # 办事 (client 2026-11-25: a photo of a meal and "计算一下卡路里" got "不算。这是你给自己找的下一件事"):
                            # something to work out or make for him
                            r"|计算|算(?:一下|一算|算|个|出|下|吧)|估(?:一下|算|个)|多少(?:卡|千卡|大卡|热量|钱|克|斤|度|分)|热量|卡路里"
                            r"|翻译|帮我(?:写|查|找|列|改|做|翻|认|总结|整理|起|想|看下|检查|润色|算)|写(?:一[份篇个段封句条]|个|段|份)"
                            r"|列(?:个|一下|一份|出)|清单|总结一下|整理一下|这是什么|是什么意思|什么意思|怎么读|怎么念|解释一下|识别"
                            r"|这道题|怎么解|换算|对比一下|比较一下|哪个(?:贵|便宜|健康|热量|划算)")
PRACTICAL_PROMPT = ("判断他最后这句话要的是哪一种回应，只输出“办事”“实事”“心事”中的一个词。\n"
                    "办事：他要你直接做一件具体的事、把结果给他：算一个数（热量、卡路里、钱、时间、分数）、认图里的东西、翻译、"
                    "解释一个词或一道题、写一段东西（周报、请假条、文案、要发给别人的消息）、列清单、整理总结、比一比两样东西。"
                    "他前面发了图、这句让你拿图做什么，也是办事。\n"
                    "实事：他在问一件实际的事该怎么办、该不该、要不要、怎么准备、怎么选、怎么谈，要你帮他分析、出主意。比如工作上的安排、"
                    "生意和合作、谈判报价、出差见人、开会提案、面试考试、找工作和跳槽的条件、学习、给孩子选学校、买东西、钱借不借、"
                    "跟人怎么沟通一件具体的事。带着一点紧张或着急也算，只要他问的是这件事怎么办。\n"
                    "心事：他在说心里的难受、委屈、累、烦、自责、迷茫，想被安慰、被理解、把情绪理清；或者是感情、家人、朋友里的伤心事"
                    "（要不要分手、要不要原谅、怎么面对父母催婚）；或者只丢来一句“想辞职”“要不要回老家”，没摆出具体条件；或者只是闲聊。")
PRACTICAL_NOTE = ("这一条是实事：他问的是一件实际的事，要你帮他拿主意。照你人格档案里【他问一件实际的事】来："
                  "先亮出你的判断，再从他的处境里说理由，再说具体怎么做，最后说什么情况下另一个选择更好。"
                  "不把选择推回给他，不收在“怎么定是你的”“你心里已经有答案了”这类话上。")
TASK_NOTE = ("这一条是办事：他要你直接办一件具体的事。照你人格档案里【他要你办一件事】来：痛快办了，结果放在最前面"
             "（算的给出每一样和总数，写的给成稿，翻译的给译文，认的说清是什么）；他要了几样就给几样，一样不落（先翻译再回一句的，"
             "先给译文，再给能直接发出去的那句，用原文的语言）。要估的说清按什么估（份量、做法），给个范围；数字用阿拉伯数字。"
             "看图写下的内容要是明显不合常理（一桌几十碗、一个人十几瓶），按常理估，说一句图上这处可能没看清。"
             "不拒绝，不说“这个不算”“这不归我管”，不把这件事往他的心事上引，也不琢磨他为什么要办这件事。")
PRACTICAL_FOLLOW = {"实事": "这是接着聊：前面给过的判断和理由不再说一遍，接着他这句往下说具体的。",
                    "办事": "这是接着聊：前面办过的不整段重来，照他这句补上、改好，结果还是放在最前面。"}
PRACTICAL_CAP = {"bange": 1500, "pingzuo": 1200}  # 字; the 元宝、豆包、Grok answers he compared ran 1500-3000
FUTURE_ASK = re.compile(r"未来|接下来|今后|往后|以后|明年|后年|(?:19|20)?\d{2}年|时间节点|时间点|哪一?年|几月|什么时候|流年|运势|走势")
FUTURE_CAP = 1600  # 字, 导师 giving 时间节点 (see reply_limits)
PRACTICAL_TOKENS = 2600

# 专项记录 · 饮食、睡眠、心情 (client 2026-11-25: meals worked out from a photo or his words "要专门在一个“饮食”数据结构中
# 长期保存记录累积下来……人格要能够查阅到这些记录"; then "做三种专项数据：饮食、睡眠、心情。都能够长期保存、积累、查阅、删除").
# Writing: after a reply, if his turn touches a meal's calories, a night's sleep or how he feels, ONE short model call in the
# background (App.note_records) pulls out what to keep; a follow-up about the same meal, night or stretch of talk updates
# that record instead of adding one. Reading (user: the selves must know when to look and when not to): only kinds he has
# records of, only when his words touch the topic (RECORD_HINT), and then a 20-token call decides (records_needed).
RECORD_HINT = {"diet": re.compile(r"吃|喝|饭|餐|菜|零食|饮料|热量|卡路里|大卡|千卡|营养|蛋白质?|碳水|脂肪|减肥|减脂|增肌|体重|饮食|控糖|血糖|胖|瘦"),
               "sleep": re.compile(r"睡|失眠|醒|熬夜|作息|午觉|午休|困|没精神|精神|累|疲|乏"),
               "mood": re.compile(r"心情|情绪|状态|最近|这几天|这周|上周|这阵子|这段时间|一直|总是|老是|好点|好多了|比以前|比上")}
RECORD_NEED_PROMPT = ("判断回他最后这句话时，要不要翻他的记录（饮食：他让你们算过的每一顿；睡眠：他说过的每一晚怎么睡的；"
                      "心情：他每次聊天时的心情）。只输出要翻的，用空格隔开，比如“饮食”或“睡眠 心情”；都不用就输出“不用”。\n"
                      "要翻：他问起以前的、问累计和变化（今天一共吃了多少、这周睡得怎么样、我最近心情是不是好点了、跟上周比）；"
                      "他在算新的一顿，顺带说今天一共多少有用；他说的状态跟吃和睡直接有关（这几天特别困、白天没精神、胖了、想控制饮食）。\n"
                      "不用：说的是别的事；只是顺嘴带到吃饭睡觉（吃醋、睡一觉就好了、累死了）；他正难受、想被安慰，没问以前怎样。\n"
                      "他最后这句要是接着前面问（那上个月呢、跟上周比呢）：翻前面问的那一样，日期按最后这句说的"
                      "（前面问这周、最后问“那上个月呢”，就翻上个月）。\n"
                      "今天是 %s（星期%s）；这周 %s，上周 %s，这个月 %s，上个月 %s。要翻的每一样后面写上翻哪几天：他说了哪段"
                      "就写哪段（这周、上个月、十月、去年、11月3号那天），没说就写最近两周 %s。比如“饮食 2026-10-01~2026-10-31”。\n"
                      "这次能翻的只有：%s。")
# "多少卡", "450 到 550 卡" too (review 2026-11-26: a reply in 卡 alone, so a photo asked about was never recorded)
DIET_CALC = re.compile(r"热量|卡路里|大卡|千卡|kcal|营养|蛋白质|碳水|脂肪|多少卡|\d\s*卡(?![片通车住])", re.I)
# what he ate said without 吃/喝 (review 2026-11-25: "我早上两个鸡蛋，中午一碗面" was never looked at): a time of day with
# an amount, or a common food. A miss loses the record; a false hit costs one background call that writes nothing.
DIET_SAID = re.compile(r"吃了|喝了|吃的|喝的|早饭|午饭|晚饭|夜宵|宵夜|早餐|午餐|晚餐|外卖|零食|一顿|加餐|吃撑|吃多了|没吃|"
                       r"(?:早上|早晨|上午|中午|下午|晚上|夜里|刚才|刚刚)[^，。！？\n]{0,6}?[一二两三四五六七八九十半几\d]+\s*"
                       r"(?:个|碗|杯|份|盘|块|片|根|瓶|罐|袋|包|只|串|勺|碟|笼|两|克|斤)|"
                       r"鸡蛋|米饭|面条|拌面|炒饭|包子|馒头|粥|饺子|馄饨|米线|米粉|面包|三明治|汉堡|披萨|炸鸡|沙拉|火锅|烧烤|麻辣烫|"
                       r"便当|盒饭|水果|牛奶|酸奶|豆浆|油条|奶茶|咖啡|可乐|啤酒|白酒|红酒|蛋糕|甜品|饼干|薯片")
# a correction of the meal he has just told, naming neither the meal nor the food (review 2026-11-26: "其实只有半碗" was
# never looked at): an amount, or a word that corrects. Only counted right after a meal (App.after_meal).
DIET_FIX = re.compile(r"[一二两三四五六七八九十半几\d]+\s*(?:个|碗|杯|份|盘|块|片|根|瓶|罐|袋|包|只|串|勺|碟|笼|两|克|斤|口)|"
                      r"一半|没吃完|剩了|剩下|少了|多了|算错|记错|说错|改成|应该是|还加了|忘了说|没那么多")
SLEEP_TALK = re.compile(r"睡|失眠|醒了|醒来|才醒|就醒|熬夜|通宵|作息|午觉|午休|打盹|噩梦|做梦|梦见|安眠|褪黑素|躺下|躺到|上床|"
                        r"起床|爬起来|赖床|闹钟")
MOOD_TALK = re.compile(r"没精神|不开心|心累|无力|撑不住|开心|高兴|快乐|幸福|满足|轻松|爽|激动|难过|伤心|委屈|烦|焦虑|紧张|害怕|担心|生气|气死|郁闷|压抑|崩溃|想哭|哭|"
                       r"累|疲|空落|丧|emo|低落|失落|孤独|寂寞|心情|情绪|难受|堵|慌|无聊|迷茫|绝望|后悔|自责|内疚|没劲|提不起", re.I)
RECORD_ASK = {
    "diet": ('"diet"：他说了自己吃了、喝了什么（哪一顿、吃了什么、多少），或者这一轮算了他自己吃的、喝的东西的热量或营养，就记；'
             "回复里算了数就照回复的数，没算的写 null，不自己估。一顿一个对象，放在一个列表里（他一句话说了早饭和午饭，就是两个）：\n"
             '[{"meal": "早饭|午饭|晚饭|加餐|夜宵|不确定", "day": 0, "items": [{"name": "拌面", "amount": "1碗，约二两", '
             '"kcal": [350, 400]}], "kcal": [1000, 1210], "protein_g": null, "fat_g": null, "carb_g": null, "note": ""}]\n'
             "items 一样一项；kcal 给了范围写范围，一个数就两头写一样，没算的写 null；外面的 kcal 是这一顿的总数，照回复里的总数，"
             "哪样没算进去在 note 里说一句。day：今天 0，昨天 -1。meal：他说了哪顿（或者早上、中午、晚上）写哪顿，没说写“不确定”，"
             "不按现在几点猜。"
             "protein_g、fat_g、carb_g：回复给了这一顿的克数才填（范围取中间）。他发来吃的、喝的照片问热量或营养，"
             "就记下这一顿（客户：拍了吃的、算了热量的都要存）；一桌好几样、看着不止一个人的量也记，note 写一句"
             "“看着是几个人的量”。他说了是别人吃的、只问一般知识（一碗米饭多少卡）、只问以前记过的（今天一共多少、"
             "中午那顿多少热量来着、这周吃得怎么样），写 null：回复只是把记过的再说一遍，不是更正，也不加 same。"),
    "sleep": ('"sleep"：他说了自己某一晚（或某次午睡）具体睡得怎么样才记：\n'
              '{"night": -1, "nap": false, "bed": "23:30", "wake": "07:10", "hours": 7.5, "quality": "好|还行|差|失眠", '
              '"wakeups": null, "note": ""}\n'
              "night：昨晚 -1，今晚、刚才、凌晨还没睡 0，前晚 -2；今天午睡写 0 并且 nap 为 true。没说的写 null，不猜；"
              "hours 他说了时长、或者几点睡几点醒都说了才写。笼统的（最近总睡不好）、别人的、只是说想睡了要去睡了，写 null。"),
    "mood": ('"mood"：他说了或者明显流露出自己这会儿（或今天）的心情才记：\n'
             '{"mood": "一个词，比如 开心 轻松 平静 一般 累 烦 焦虑 紧张 低落 难过 委屈 生气 孤独 迷茫 崩溃", '
             '"score": 3, "about": "因为什么，十个字以内", "note": ""}\n'
             "score：1 很糟，2 不好，3 一般，4 不错，5 很好。about 他没说写 null。只是在问事、办事，看不出心情，写 null。"),
}
RECORD_DIET_SAME = ("\n他今天、昨天记下的几顿和刚说过的（编号. 哪天 哪顿（几点说的）吃了什么）：\n%s\n这一轮要是还在说上面其中一顿（补算、改份量、"
                    "加了一样），diet 里这一顿写完整的新记录（它原来有的加上他这次改的、补的；上面另外列着的别的几顿里的东西不要并进来，"
                    "回复放在一起说了也不并），day 写那一顿是哪天，并加上 \"same\": 那一顿的编号（他说了是午饭，就是午饭那一顿；"
                    "说了是哪天，就是那天的）；说的是另一顿（又吃了一顿也是另一顿）就不加。")
# always asked with diet: a correction of a meal not on that list (an older day) is flagged too, and found by its day
RECORD_DIET_FIX = ("\n他要是在更正以前记过的一顿（上面没列的也算，比如“前天午饭其实只有半碗”），diet 里写这一顿完整的新记录，"
                   "day 写那一顿是哪天，加上 \"same\": true。")


def day_word(d, today):
    try:
        k = (today - datetime.date.fromisoformat(d)).days
    except (TypeError, ValueError):
        return ""
    return {0: "今天", 1: "昨天", 2: "前天"}.get(k, d[5:])


def recent_meal_line(i, m, today):
    """One of the meals offered to RECORD_DIET_SAME (today = the day he sent the turn); one he deleted shows only which
    meal it was."""
    day = day_word(m.get("d"), today)
    if m.get("gone"):
        return "%d. %s %s（%s 说的；这一顿他已经删掉了）" % (i, day, m.get("meal") or "一顿", str(m.get("t", ""))[11:16])
    return "%d. %s %s" % (i, day, meal_line(m))


def _dishes(r):
    return [n for n in (str(i.get("name") or "").strip() for i in r.get("items") or [] if isinstance(i, dict)) if n]


def same_meal(got, rec, recent, used, more=()):
    """-> (which earlier meal this extracted one updates, or None; whether it is a correction). recent = the meals the
    model was shown (record_recent, numbered from 1); more = the meals of the day he named when that is before
    yesterday (App.day_meals). A correction that fits none is kept out of the day's sums (note_records), so no meal
    counts twice; not a correction ("false", 0, null), or no meal on that day that could be it = a meal of its own.
    Among the meals of the day he named (any day when he named none), of the meal he named (or any, with none named):
    the one of that meal; else the one sharing a dish (拌面 / 葱油拌面); else the model's number, or for true the one
    left (of that day, when there are several; never a guess). A meal he didn't say the meal of, with no dish in common,
    isn't taken for the one he named. Each one is updated once a turn. (Reviews 2026-11-26: "午饭其实只有半碗" went onto 早饭, then onto a 牛奶
    said later; "刚才说的拌面其实只吃了半碗" onto the 牛奶; "昨天午饭只有半碗" onto today's; one told more than 3 hours
    before came in as a second meal.)"""
    s = got.get("same")
    if str(s).strip().lower() == "true":  # True too
        n = 0
    elif isinstance(s, (int, str)) and not isinstance(s, bool) and str(s).strip().isdigit() and int(s) > 0:
        n = int(s)
    else:
        return None, False
    meal, ids = rec.get("meal"), {m["id"] for m in recent}
    pool = list(recent) + [m for m in more if m["id"] not in ids]
    # he said which day: that day's only. 0 is also what the model writes when he said none, so today counts as said
    # only with 今天 in his words (review 2026-11-26: "说的是今天午饭" went onto yesterday's, the only one there was)
    if _day(got.get("day")) or TODAY_SAID.search(rec.get("said") or ""):
        pool = [m for m in pool if m.get("d") == rec.get("d")]
    pool = [m for m in pool if not meal or m.get("meal") in (None, meal)]
    if not pool:
        return None, False  # nothing it could be: another meal (晚饭 after 早饭 and 午饭), or a day with none
    free = [m for m in pool if m["id"] not in used]
    mine = _dishes(rec)
    shares = lambda m: any(a in b or b in a for a in _dishes(m) for b in mine)
    named = [m for m in free if meal and m.get("meal") == meal]
    dish = [m for m in named or free if shares(m)]
    for one in (named, dish):
        if len(one) == 1:
            return one[0], True
    choose = dish or named or free
    if n:
        pointed = recent[n - 1] if n <= len(recent) else None
        pick = pointed if any(pointed is m for m in choose) else None
    else:
        today = [m for m in choose if m.get("d") == rec.get("d")]  # true alone: only when that leaves one, never a guess
        pick = today[0] if len(today) == 1 else choose[0] if len(choose) == 1 else None
    if pick and meal and pick.get("meal") != meal and not shares(pick):
        pick = None  # a meal he didn't say the meal of, with nothing in common with this one
    return pick, True


RECORD_TURN = ("下面是一个人刚才的一轮对话：他发来的（图片里的内容和他打的字）和“另一个自己”的回复。把他在这一轮里给出的、"
               "关于他自己的下面几样信息提炼出来。只输出一个 JSON 对象，最外层的键就是 %s，每个键的值是下面那样的对象，哪样没有就写 null：\n")
MEALS = ("早饭", "午饭", "晚饭", "加餐", "夜宵")
# The page's own replies whose words still go into his records (review 2026-11-25: 导师's fixed answer to
# "我只想说说，今天真的很难过" never reached /api/chat, so that day's mood went unrecorded). Not crisis: that line is a
# safety script, not talk to keep a mood from.
LOCAL_RECORDED = ("fixed", "llm-failed", "offline")


def records_needed(users, have, ask):
    """Which of his records (kinds in `have`) this reply should be able to look at (see RECORD_NEED_PROMPT), and for
    which days: {kind: (since, until) | None (the last RECORD_DAYS days)}. A short follow-up ("那上个月呢") is
    judged with the line before it (review 2026-11-25: it had no word about sleep, so the lookup was skipped)."""
    last = users[-1] if users else ""
    if not last or last.startswith(IMAGE_MARK):
        return {}
    prev = users[-2] if len(users) > 1 and len(last) <= 12 else ""
    hit = [k for k in have if k in RECORD_HINT and (RECORD_HINT[k].search(last) or RECORD_HINT[k].search(prev))]
    if not hit:
        return {}
    today = datetime.date.today()
    said = "\n".join("他：" + u for u in users[-3:])
    # the spans worked out here: left to the model, "那上个月呢" came back as this month one time in three
    span = lambda a, b: "%s~%s" % (a.isoformat(), b.isoformat())
    monday, first = today - datetime.timedelta(days=today.weekday()), today.replace(day=1)
    last_end = first - datetime.timedelta(days=1)
    prompt = RECORD_NEED_PROMPT % (today.isoformat(), "一二三四五六日"[today.weekday()], span(monday, today),
                                   span(monday - datetime.timedelta(days=7), monday - datetime.timedelta(days=1)),
                                   span(first, today), span(last_end.replace(day=1), last_end),
                                   span(today - datetime.timedelta(days=RECORD_DAYS - 1), today),
                                   "、".join(RECORD_NAMES[k] for k in hit))
    try:  # a range for every kind, even when he named none: asked for one only for a stretch, it often left it out
        got = ask([{"role": "system", "content": prompt}, {"role": "user", "content": said}], 60) or ""
    except LLMError:
        return {}
    out = {}
    for k in hit:
        m = re.search(RECORD_NAMES[k] + r"(?:\s*(\d{4}-\d{1,2}-\d{1,2})(?:\s*(?:~|～|到|至)\s*(\d{4}-\d{1,2}-\d{1,2}))?)?", got)
        if m:  # "饮食 2026-10-01~2026-10-31", or one day "饮食 2026-11-03"
            try:
                out[k] = tuple(sorted(datetime.date(*map(int, x.split("-"))) for x in (m.group(1), m.group(2) or m.group(1)))) \
                    if m.group(1) else None
            except ValueError:
                out[k] = None
            if k == "sleep" and out[k] and out[k][0] == today:  # a night is kept under the evening it began: "我昨晚睡了
                out[k] = (today - datetime.timedelta(days=1), out[k][1])  # 几个小时" came back as today only and found none
    return out


def record_kinds_said(turn, reply, routed, after_meal=lambda: False):
    """Which records this turn may hold (cheap patterns; the model call in App.note_records decides). after_meal() ->
    whether he has just told a meal (App.after_meal): then an amount or a correction alone counts too (DIET_FIX)."""
    words = " ".join(t for t in turn if not t.startswith(IMAGE_MARK))
    diet = DIET_CALC.search(reply) or DIET_CALC.search(" ".join(turn)) or DIET_SAID.search(words) or \
        (DIET_FIX.search(words) and after_meal())
    return [k for k, hit in (("diet", diet), ("sleep", SLEEP_TALK.search(words)), ("mood", routed or MOOD_TALK.search(words)))
            if hit]


def record_entry(kind, got, pid, tid, turn, now):
    """The model's JSON for one kind (one meal) -> a record (memory.Memory.record_put), or None when there is nothing to
    keep. now = when he sent the turn."""
    if not isinstance(got, dict):
        return None
    words = [t for t in turn if not t.startswith(IMAGE_MARK)]
    base = {"t": now.isoformat(timespec="seconds"), "p": pid, "tid": tid, "said": clip(" ".join(words), 120) or None,
            "note": clip(got.get("note"), 60) or None}
    if kind == "diet":
        items = [{"name": clip(it["name"], 20), "amount": clip(it.get("amount"), 30) or None, "kcal": parse_kcal(it.get("kcal"))}
                 for it in got.get("items") or [] if isinstance(it, dict) and str(it.get("name") or "").strip()]
        if not items:
            return None
        lost, day = sum(t.startswith(IMAGE_LOST) for t in turn), _day(got.get("day"))
        if day is None:
            return None
        # meal: what he said, or None. Not the hour: a meal told in the evening was being kept as 晚饭 (review 2026-11-25)
        rec = dict(base, d=(now.date() + datetime.timedelta(days=day)).isoformat(),
                   meal=got.get("meal") if got.get("meal") in MEALS else None, items=items, kcal=parse_kcal(got.get("kcal")),
                   pics=sum(t.startswith(IMAGE_MARK) for t in turn) - lost, lost=lost or None)
        for k in ("protein_g", "fat_g", "carb_g"):
            v = parse_kcal(got.get(k))
            if v:
                rec[k] = round(sum(v) / 2)
        return rec
    if kind == "sleep":
        clock = lambda x: x.strip() if isinstance(x, str) and re.fullmatch(r"\s*[0-2]?\d:[0-5]\d\s*", x) else None
        hours = got.get("hours") if isinstance(got.get("hours"), (int, float)) and 0 < got["hours"] <= 24 else None
        if got.get("nap"):  # a nap goes into that day's record
            return dict(base, d=now.date().isoformat(), nap="午睡约 %g 小时" % hours if hours else "睡了午觉")
        quality = got.get("quality") if got.get("quality") in ("好", "还行", "差", "失眠") else None
        bed, wake = clock(got.get("bed")), clock(got.get("wake"))
        wakeups = got["wakeups"] if isinstance(got.get("wakeups"), int) and 0 <= got["wakeups"] < 30 else None
        if not (bed or wake or hours or quality or wakeups is not None or base["note"]):  # "半夜还醒了两次" alone counts
            return None
        # "昨晚" is -1 whenever he says it; only "今晚、刚才、还没睡" (0) said before 5 am is the night that began yesterday
        # (review 2026-11-25: at 2 am "昨晚 23 点睡" was moved back twice, to the 23rd)
        n = _day(got.get("night"))
        if n is None:
            return None
        night = now.date() + datetime.timedelta(days=n - (1 if n == 0 and now.hour < 5 else 0))
        return dict(base, d=night.isoformat(), bed=bed, wake=wake, hours=hours, quality=quality, wakeups=wakeups)
    mood = clip(got.get("mood"), 6)
    if not mood:
        return None
    score = got.get("score") if isinstance(got.get("score"), int) and 1 <= got["score"] <= 5 else None
    return dict(base, d=now.date().isoformat(), mood=mood, score=score, about=clip(got.get("about"), 16) or None)


RECORD_BACK = 366  # how many days back a meal or a night can be told, or corrected
TODAY_SAID = re.compile(r"今(?:天|早|晚|儿|日)")


def _day(x):
    """The model's day or night (0 today, -1 yesterday; none given = today) -> the offset, or None when it isn't one of
    the last RECORD_BACK days: then nothing is written, rather than onto another day (review 2026-11-26: a correction
    with day -9 was cut to -7 and went onto that day's meal)."""
    if x is None or x == "":
        return 0
    try:
        k = int(x)
    except (TypeError, ValueError):
        return None
    return k if -RECORD_BACK <= k <= 0 else None


STEP_INLINE = re.compile(r"(?<=[。！？；：:])[ \t]*(?=[1-9][.．、](?!\d)|[一二三四五六七八九][，、](?![一二三四五六七八九]))")


LEAD_IN = re.compile(r"^(?:先说|先给|先亮|直接说|结论先说)(?:我的|你)?(?:判断|结论|看法)?(?:吧|是)?[：:，,]\s*")


def practical_tidy(reply):
    """An answer to a practical question: no hand-back ending (HANDBACK), no announcer's lead-in ("先说判断："), and
    steps run into one paragraph ("具体这么弄。1. …。2. …") each start a line, as the prompt asks."""
    reply = LEAD_IN.sub("", drop_stock_end(reply, HANDBACK))
    return "\n".join(STEP_INLINE.sub("\n", p) if len(STEP_INLINE.findall(p)) >= 2 else p for p in reply.split("\n"))


def practical_ask(users, ask):
    """"办事" when his last line asks for something worked out or made for him, "实事" when it lays out a practical matter
    and asks what to do, else None (see PRACTICAL_PROMPT). A picture sent with no words is not an ask."""
    last = users[-1].strip() if users else ""
    if len(last) < 4 or last.startswith(IMAGE_MARK) or astro_turn(users) or not PRACTICAL_HINT.search(last):
        return None
    said = "\n".join("他：" + u for u in users[-3:])
    try:
        got = ask([{"role": "system", "content": PRACTICAL_PROMPT}, {"role": "user", "content": said}]) or ""
    except LLMError:
        return None
    return next((k for k in ("办事", "实事") if k in got), None)


def views_block(pid, ids):
    cards = [view_library(pid)[i] for i in ids if i in view_library(pid)]
    if not cards:
        return ""
    out = ["【可以借的看法】下面是跟他这件事有关的一些看法和说话思路，是你说话方式的参考来源，不是你的经历。"
           "合适就挑一两点，用你自己的话、按他说的事说出来；不合适就不用。不照抄句子，不提出处，不说“有人说过”“很多人觉得”，不讲任何人的经历。"
           "里面的场景、细节、例子都是别人的，不能安到他头上：说到他的事，只用他自己说过的，他没说过的（比如搬没搬过家、生没生过病、无不无聊）不替他补。"]
    untimed = lambda s: re.sub(r"今晚|今天|今夜|明天|昨天", "", s)
    for c in cards:  # no example lines: the model copied them word for word ("给自己弄口热的吃")
        card = ("话题：%s\n看法：%s\n思路：%s\n听的人被戳中的地方（只帮你拿捏分寸，不转述，不安到他身上）：%s"
                % (c["topic"], untimed(c["view"]), c["move"], untimed(c["resonance"])))
        if c.get("feels"):
            card += "\n这种难受常见的样子（帮你把他的感受说准；他没说过的不安到他身上）：" + untimed(c["feels"])
        if c.get("eases"):  # two at random: the same topic shouldn't always get the same comfort
            eases = random.sample(c["eases"], min(2, len(c["eases"])))
            card += ("\n同样处境的普通人互相宽慰时，最让人松口气的意思（竖线前是做法）。这是提要，不是台词："
                     "最多借一个，从他说的事里找词，用你自己的大白话重新说，别用这里的原词原句，也别说成金句：\n"
                     + "\n".join("- " + untimed(e) for e in eases))
        out.append(card)
    return "\n\n".join(out)


def system_prompt(pid, *blocks):
    """Every endpoint's system prompt: persona, then the blocks it opts into, then the output rule. With test code names
    on, the prompt files' 半格/平坐 become the display names and the persona is told what its name does not mean."""
    text = "\n\n".join([persona_prompt(pid)] + [b for b in blocks if b] + [OUTPUT_RULE])
    if not ALIAS:
        return text
    for pp, base in BASE_NAMES.items():
        text = text.replace(base, ALIAS[pp])
    return ALIAS_NOTE % (ALIAS[pid], NAME_NOTE.get(ALIAS[pid], "")) + "\n\n" + text


def user_block(req):
    return "【这位用户】\n称呼：%s" % (clip(req.get("nick"), 12) or "朋友")


def plan_reply(pid, hist, ask, avoid=(), have=()):
    """What /api/chat settles before writing a reply (tools/run_eval.py and chatlog replay use it too, so they match):
    whether this is astrology or a practical ask, which view cards, which route, which of his records (kinds in `have`)
    to look at. ask(msgs) -> text, a short model call."""
    users = [clip(h.get("content"), 300) for h in hist[-6:] if h.get("role") == "user"]
    astro = astro_now(hist)
    practical = not astro and practical_ask(users, ask)
    last_ai = next((str(h.get("content") or "") for h in reversed(hist) if h.get("role") != "user"), "")
    return {"users": users, "astro": astro, "practical": practical,
            "views": [] if astro or practical else pick_views(pid, users, ask),  # the view cards are about feelings
            "move": None if astro or practical else pick_move(pid, users, last_ai, list(avoid), ask),
            "records": {} if astro else records_needed(users, have, ask)}


def reply_limits(pid, plan, cap):
    """(max_tokens, cap in 字) for this reply; cap = what memory.DEPTH allows for this user."""
    if plan["practical"]:
        return PRACTICAL_TOKENS, PRACTICAL_CAP[pid]
    # 导师's 时间节点 run 1300-2100 字; cut to a new user's 900, whole years went missing (the client wants every year)
    if pid == "bange" and plan["astro"] and plan["users"] and FUTURE_ASK.search(plan["users"][-1]):
        return PRACTICAL_TOKENS, FUTURE_CAP
    # a full read of his chart / type: the "just met" length limit is for comfort, not for this
    return CHAT_MAX_TOKENS, REPLY_MAX if deep_read(plan["users"]) else cap


def finish_reply(pid, plan, msgs, raw, cap, told=False):
    """The model's text -> what he sees: length cap, safety lines, stock endings, the practical tidy, no forecasts."""
    reply = postprocess(msgs[-1]["content"], fit(raw, cap), " ".join(plan["users"]), told, pid)
    if plan["practical"]:
        reply = practical_tidy(reply)
    if plan["astro"] and (PREDICT.search(reply) or re.search(BODY, reply)):
        reply = drop_sentences(reply, PREDICT.pattern + "|" + BODY)
    return reply


def build_messages(pid, req, know=None, views="", move=None, saved=None, practical=False):
    """Chat: persona + who the user is (+ what the selves know) (+ view cards for what he is talking about) + the last
    16 turns. saved = birth details he just gave in chat that are now in req["birth"] (see said_birth); practical = "实事"
    when he asks what to do about a practical matter, "办事" when he wants something worked out or made (see practical_ask)."""
    ctx = [user_block(req)]
    # The page writes the summary to him ("你习惯边说边想……"); handed over like that, the selves quoted it back as something
    # he had said ("你上回说过，你习惯边说边想"). Third person makes it a description, not his words.
    prof = clip(req.get("profile"), 800).replace("你", "他")
    if prof:
        ctx.append("性格画像（他填的资料推出来的，只当线索，不当结论；不是他跟你说过的话，别说成“你提过”“你说过”；"
                   "不要逐条复述，也不要主动提星座、MBTI 这些词，除非对方先提）：\n" + prof)
    notme = clip(req.get("notme"), 200)
    if notme:
        ctx.append("用户标了“不像我”的画像条目（不要再拿这些说事）：" + notme)
    ctx.append(know or NEW_USER_LINE)
    hist = req.get("history") or []
    if not hist:
        raise ValueError("empty history")
    last_ai =next((str(h.get("content") or "") for h in reversed(hist) if h.get("role") != "user"), "")
    astro = astro_now(hist)
    if astro:
        hist = astro_run(hist)
    last = str(hist[-1].get("content") or "") if hist else ""
    deep = astro and deep_read([str(h.get("content") or "") for h in hist if h.get("role") == "user"])
    chart = birth_chart(req.get("birth")) if astro else None
    # only while he is on it (otherwise it creeps into everything), and only the systems he is asking about; an MBTI-only
    # question ("infj和哪种mbti最适配") gets no chart
    parts = chart_parts([str(h.get("content") or "") for h in hist if h.get("role") == "user"]) \
        if ASTRO.search(last) or ANALYZE.search(last) or not TYPING.search(last) else []
    if chart and parts:
        ctx.append(paipan.chart_text(chart, parts))
        if pid == "bange":  # the years 导师 gives 时间节点 from (see FUTURE_NOTE)
            ctx.append(paipan.years_text(chart, datetime.date.today(), parts=parts))
    if astro and (TYPING.search(last) or deep):
        code = MBTI_TYPE.search(last) or MBTI_TYPE.search(clip(req.get("profile"), 800))
        if code:
            ctx.append(mbti_block(code.group(1)))
    if views:
        ctx.append(views)
    # The selves don't know about each other (user 2026-11-23): no name of, or referral to, the other one.
    now = "【此刻】日期 %s（你心里有数就行，回复里不说今天、今晚这类时间词）。" % datetime.date.today().isoformat()
    if astro:
        now += ASTRO_NOTE + FUTURE_NOTE[pid] + (ANALYZE_NOTE if deep else "")
    if saved:
        now += SAVED_NOTE % "、".join(BIRTH_FIELD[k] + saved[k] for k in BIRTH_FIELD if saved.get(k))
    if chart and parts:
        stale, hist = stale_claims(chart, hist)
        now += stale
    # pictures come as their own turns, then his words if he typed any (several pictures and one line, one reply)
    turn = list(itertools.takewhile(lambda h: h.get("role") == "user", reversed(hist)))
    if any(str(h.get("content") or "").startswith(IMAGE_MARK) for h in turn):
        now += IMAGE_NOTE + (IMAGE_LOST_NOTE if any(str(h.get("content") or "").startswith(IMAGE_LOST) for h in turn) else "")
    full = req.get("history") or []
    if full and any(same_line(full[-1].get("content"), h.get("content")) for h in full[:-1] if h.get("role") == "user"):
        now += REPEAT_NOTE
    tics, said = used_endings(hist), [k for k, rx in TICS.items() if rx.search(last_ai)]
    if tics:
        now += "你前面已经拿“%s”收过尾了，这条别再这么收，也别换个说法说同一个意思。" % "”“".join(tics)
    if said:
        now += "你上一条用过“%s”，这一条别再用这些说法。" % "”“".join(said)
    # 接着聊 (user 2026-11-24 "回答还是比较公式化"): after a long reply, the next one used to start the whole arc again
    # (陪 → 列他那几件事 → 不是……是…… → 许可) and list his troubles a second time.
    follow = len(last_ai) >= 150 and not astro
    route = moves(pid).get(move) if move else None
    if practical:
        now += (TASK_NOTE if practical == "办事" else PRACTICAL_NOTE) + (PRACTICAL_FOLLOW[practical] if follow else "")
        follow, route = False, None
    if follow:
        now += ("这是接着聊：你上一条已经说了一大段，这一条接着他刚说的这一句往下走，上一条说过的（他的处境、那几件事、"
                "你的安慰、看法和给过的建议）不再说一遍，也不再从头陪一遍、理一遍。" + ("" if route else random.choice(FOLLOW_SHAPES[pid]) + "。"))
    if route and follow and move != "m8":
        now += ("这一条从【%s】的角度接着说，一两段就够，不用把这条路子的每一步都走到，也不用收在一句金句上。" % route["name"])
    elif route:
        now += ("这一条走【%s】这条路子：%s只走这一条，别把别的路子串进来，也不用每条都收在一句金句上。"
                % (route["name"], route["how"]))
    if not astro and len(str(hist[-1].get("content") or "")) >= 6:  # the most worn device; the style rule alone didn't hold
        now += "这一条里“不是……是……”这种先否定再翻过来的句子最多一处，其余的直接说是什么。"
    if pid == "pingzuo":  # 客户：“说长一点，帮我分析化解情绪”；伙伴的档案处处说慢、说短，这里再把长度提一次
        now += "这一条不提问，不用问号。" + ("" if astro or follow or practical else
               "他要是带着情绪、讲了一件压着他的事，就说长一点，说够四五段、每段三四句："
               "先陪着，再把他这股情绪理一理，再帮他化开一点；他只是随口一句，就短短地回。")
    elif "？" in last_ai or "?" in last_ai:  # users: "我说一句问我一句，更累了"
        said = str(hist[-1].get("content") or "").strip() if hist else ""
        now += ("他只回了“%s”，是在答你上一条的问题，不想被接着追问。" % said if len(said) <= 10 else "你上一条问过他了，他也答了。") \
            + "这一条不提问，不用问号，也不说“我不问了”这类话：顺着他说的，把你的看法说出来，用一句判断收住。"
    ctx.append(now)
    msgs = [{"role": "system", "content": system_prompt(pid, "\n".join(ctx))}]
    for h in hist[-16:]:
        role = "user" if h.get("role") == "user" else "assistant"
        content = clip(h.get("content"), HIST_CLIP[role])
        if content:
            if msgs[-1]["role"] == role:
                if role == "user" and IMAGE_MARK in msgs[-1]["content"] and not content.startswith(IMAGE_MARK):
                    content = WORDS_MARK + content  # what he typed under his pictures, not more of what is in them
                msgs[-1]["content"] += "\n" + content  # merge consecutive same-role turns
            else:
                msgs.append({"role": role, "content": content})
    if msgs[-1]["role"] != "user":
        raise ValueError("last message must be from the user")
    msgs[-1]["content"] = strip_style_ask(msgs[-1]["content"])
    return msgs


def portrait_facts(sign, mbti, blood, animal, chart=None):
    """What the portrait read may use: what he filled in, plus (with a birth date) the computed chart and the MBTI
    function stack, so the read goes past "狮子座：火象，直接" (user 2026-11-24: "浅尝辄止")."""
    lines = ["星座：%s座（%s象）" % (sign, SIGNS[sign]) if sign else "", "MBTI：" + mbti if mbti else "",
             "血型：%s 型" % blood if blood else "", "生肖：" + animal if animal else ""]
    if mbti in STACK:
        f = STACK[mbti].split()
        lines.append("MBTI 认知功能（从强到弱）：主导 %s，辅助 %s，第三 %s，劣势 %s。%s" % (*f, "；".join(
            "%s=%s" % (x, FUNC[x]) for x in f)))
    if chart:
        w, b, z = chart["west"], chart["bazi"], chart["ziwei"]
        bodies = "、".join("%s%s%d°%s" % (x["name"], x["sign"], x["deg"], "（第%d宫）" % x["house"] if "house" in x else "")
                          for x in w["bodies"] if x["sign"])
        asc = ("上升%s" % w["asc"]["sign"]) if w["asc"] else ("上升可能是%s" % "、".join(w["asc_range"]) if w.get("asc_range") else "")
        lines.append("星盘：%s%s。元素 %s。主要相位：%s。" % (bodies, "；" + asc if asc else "", w["elements"],
                                                   "、".join(w["aspects"][:5]) or "无"))
        lines.append("八字：%s，日主%s，粗看%s；五行 %s；十神分布 %s。" % (" ".join(b["pillars"]), b["dm"], b["strength"],
                                                         " ".join("%s%d" % kv for kv in b["wuxing"].items()), b["god_count"]))
        if z:
            lines.append("紫微斗数：命宫在%s，主星%s；三方四正：%s。" % (
                z["palaces"][0]["branch"], "、".join(z["palaces"][0]["main"]) or "无（借对宫）",
                "；".join("%s %s" % (z["palaces"][i]["name"], "、".join(z["palaces"][i]["main"]) or "无主星") for i in (4, 8, 6))))
    return "\n".join(x for x in lines if x)


def build_portrait(facts, heard=None):
    """性格画像的展开. Two separate asks, so what he said never gets passed off as astrology: the sign/MBTI read sees
    only what he filled in (-> one JSON object); `heard` (his memory) goes to its own short paragraph."""
    if heard is None:
        return [{"role": "system", "content": _read(PORTRAIT_PATH)}, {"role": "user", "content": facts}]
    return [{"role": "system", "content": _read(PORTRAIT_HEARD_PATH)},
            {"role": "user", "content": (facts + "\n" if facts else "") + "他说过的事：\n" + heard}]


def tidy_portrait(data, heard):
    def facets(v):
        return [{"h": clip(f.get("h"), 12), "t": clip(f.get("t"), 220)} for f in (v if isinstance(v, list) else [])[:5]
                if isinstance(f, dict) and f.get("h") and f.get("t")]
    return {"sign": facets(data.get("sign")), "mbti": facets(data.get("mbti")), "astro": facets(data.get("astro")),
            "bazi": facets(data.get("bazi")), "ziwei": facets(data.get("ziwei")), "mix": clip(data.get("mix"), 320),
            "heard": clip(re.sub(r"^从你说过的话里?看[，,：:]?", "", heard.strip("“”\"' \n")), 360)}  # the card already says it


CLOSING_TASK = ("【现在】用户要结束这次聊天了。用你（%s）的口吻，对用户说最后一句话，把今天聊的东西收住。"
                "硬性要求：只说一句，不超过 40 个字；必须落在用户今天亲口说过的具体内容上，别人套不上；"
                "不给建议，不提问，不用感叹号，不用表情，不说“今天聊了很多”“希望你”“加油”“一切都会好”这类话。")


def own_sections(text, pid):
    """Shared prompt files keep each self's part between <!-- pid --> and <!-- /pid -->: keep this self's, drop the
    other's, so neither self reads the other's name or ways."""
    for p in PERSONAS:
        text = re.sub(r"<!-- %s -->\n?(.*?)<!-- /%s -->\n?" % (p, p), r"\1" if p == pid else "", text, flags=re.S)
    return text


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
    system = system_prompt(pid, own_sections(_read(CLOSING_PATH), pid).strip(), user_block(req), CLOSING_TASK % PERSONAS[pid][0])
    user = "这是这次的聊天记录：\n" + "\n".join(lines) + "\n\n用户要走了。说你的最后一句。"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], "\n".join(said)


def strip_style_ask(text):
    """Drop "请用XX的口吻" from what he said (see STYLE_ASK); keep the text as is if nothing else would be left."""
    def cut(m):
        asked_self = not m.group(0).startswith(("用", "按", "学", "模仿", "以"))
        return "" if asked_self or STYLE_WHO.search(m.group(1)) else m.group(0)
    out = STYLE_ASK.sub(cut, text).strip(" ，,")
    return out if re.search(r"[一-龥A-Za-z]", out) else text


@functools.lru_cache(maxsize=None)
def prompt_version(pid):
    """Short hash of everything static in a persona's prompt; logged so replies can be compared across prompt edits."""
    return hashlib.sha1(system_prompt(pid).encode("utf-8")).hexdigest()[:8]


def fit(text, cap=REPLY_MAX):
    """Hard length cap in 字 (whitespace not counted). Keeps whole paragraphs from the top plus the last one, where a
    long reply lands; if the first paragraph alone is too long, cuts it at the last sentence end that fits."""
    size = lambda s: len(re.sub(r"\s", "", s))
    if size(text) <= cap:
        return text
    sep = "\n\n" if "\n\n" in text else "\n"
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    last = paras.pop() if len(paras) > 1 and size(paras[-1]) <= cap // 3 else ""
    kept, n = [], size(last)
    for p in paras:
        if n + size(p) > cap:
            break
        kept.append(p)
        n += size(p)
    if not kept:
        head, n = paras[0], size(last)
        ends = [i + 1 for i, ch in enumerate(head) if ch in "。！？…" and n + size(head[:i + 1]) <= cap]
        kept = [head[:ends[-1]] if ends else head[:cap - n]]
    return sep.join(kept + ([last] if last else []))


def tidy_closing(text):
    """First line only, without quotes; cut long ones at a sentence break (the prompt asks for <= 40 字)."""
    text = text.strip().strip("“”\"「」")
    text = text.splitlines()[0].strip() if text else ""
    if len(text) > 60:
        cut = max(text.rfind("。", 0, 60), text.rfind("，", 0, 60))
        text = text[:cut + 1] if cut > 10 else text[:60]
    return text


# Opening line. Client 2026-11-24: "为什么一直要结合昨天的问题来聊，要怎么开始新话题", "老是在说昨天的事" — 42 of 47
# openings had started from an old topic ("上回你说……，我一直记着"). Now only what he said in the last hour (to either
# self: he switched mid-talk) is picked up; any later visit is a plain hello that leaves the topic to him, and the
# memory only shows when he brings something up.
OPENING_CONT = ("【现在】他刚才还在说话，现在打开了和你（%s）的对话，还没开口。他刚才说的（就当是刚刚跟你说的）：\n%s\n"
                "你先开口：一到两句，不超过四十个字。接着他刚才说的最后那件事说一句，让他知道你听见了；%s。"
                "不说“上回”“我一直记着”，不说这话是他跟谁说的，不复述一大段。")
OPENING_FRESH = ("【现在】他又来了，打开了和你（%s）的对话，还没开口。你先开口：一两句，不超过三十个字，像又见到他时打个招呼。"
                 "这一句不提以前聊过的任何事（不说上回、上次、那件事、一直记着、好点没），也不猜他今天怎么了，把话头留给他：%s。")
# 伙伴 doesn't ask (users said being asked every line made them more tired), and one fixed wish ended every opening the
# same way (client chat 2026-11-23: "说一半也行" six times in a day): one gist at random, not wording.
OPENING_END = {"bange": {"cont": ["也让他知道想换个别的说也行"],
                         "fresh": ["可以问一个好回答的小问题，问他此刻，不问以前的事", "让他知道想聊什么都行，新的事、随便什么都可以"]},
               "pingzuo": {"cont": ["再让他知道接着说这个、想说点别的都行"],
                           "fresh": ["让他知道想聊什么都行，大事小事、新的事都可以", "让他知道不用急着开口",
                                     "让他知道不想说也可以就待一会儿", "让他知道你在这儿"]}}
OPENING_FOLLOW = ("【现在】他又来了，打开了和你（%s）的对话，还没开口。首页上给他留了一句话，他是点着这句进来的。他前几天说过：%s"
                  "（这件事现在应该有结果了）。你先开口：一两句，不超过四十个字，就这件事说一句，%s。"
                  "不说“上回”“上次你说”“我一直记着”“我记得你说过”，不复述细节，不猜结果好坏（不说“过去了就过去了”“别多想”这类像是已经知道结果不好的话），不说恭喜也不说没关系；"
                  "他不想聊这个，换别的也行，别让他觉得非答不可。")
FOLLOW_MAX = 40  # 字 (fit: whitespace not counted), as OPENING_FOLLOW asks
OPENING_FOLLOW_END = {"bange": "可以问一句怎么样了，只问这一个问题",
                      "pingzuo": "提一句那件事，让他知道想说说就说，不想说也没关系"}
OPENING_NOTE = {"pingzuo": "，用你自己的话，不说“说一半也行”，别总拿“嗯，我在”“想说什么都行”起头，不提问，不用问号"}


def build_opening(pid, req, know=None, just=None, follow=None):
    """开场一句: persona + who the user is + task. follow = the thing he tapped the red dot for (one line about it);
    just = his lines from the last hour (pick that up); otherwise a fresh hello, and `know` only says how long they
    have known each other (the old topics stay out of it)."""
    if follow:
        task = OPENING_FOLLOW % (PERSONAS[pid][0], follow["text"], OPENING_FOLLOW_END[pid] + OPENING_NOTE.get(pid, ""))
    else:
        kind = "cont" if just else "fresh"
        end = random.choice(OPENING_END[pid][kind]) + OPENING_NOTE.get(pid, "")
        task = (OPENING_CONT % (PERSONAS[pid][0], "\n".join("他：" + clip(t, 150) for t in just), end) if just
                else OPENING_FRESH % (PERSONAS[pid][0], end))
    system = system_prompt(pid, user_block(req), know or NEW_USER_LINE, task)
    return [{"role": "system", "content": system}, {"role": "user", "content": "（他打开了和你的对话）"}]


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
        self.fold_pool = ThreadPoolExecutor(max_workers=2)  # memory folds and records, in the background
        self.rec_jobs, self.rec_lock = {}, threading.Lock()  # uid -> his record jobs not done yet, oldest first (queue_records)
        self.mem = Memory(os.path.join(DATA_DIR, "users"),
                          lambda msgs, max_tokens, temperature: self.llm(msgs, max_tokens, temperature, timeout=40)[0],
                          follow=FOLLOW_ON, unsafe=CRISIS.search)
        self.lock = threading.Lock()       # rate limits
        self.recent_moves = {}             # (uid, pid) -> the last two 路子 used, so the next reply takes another
        self.geo = Geo(os.path.join(DATA_DIR, "geo.json"))
        self.disk_lock = threading.Lock()  # usage.json and the jsonl logs
        self.hits = {k: defaultdict(deque) for k in ("tts", "chat", "asr", "fb", "image")}
        for d in (CACHE_DIR, LOG_DIR):
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
        limit = {"tts": PER_IP_PER_MIN, "chat": CHAT_PER_MIN, "asr": ASR_PER_MIN, "fb": FB_PER_MIN, "image": IMAGE_PER_MIN}[kind]
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

    def chat(self, pid, req, who):
        """/api/chat. While the reply is being written his words aren't on disk yet: no red dot meanwhile (Memory.talk)."""
        with self.mem.talk(req.get("u") if valid_uid(req.get("u")) else None):
            return self._chat(pid, req, who)

    def _chat(self, pid, req, who):
        uid = req.get("u") if valid_uid(req.get("u")) else None
        at = datetime.datetime.now()  # when he said it: records pulled out of this turn are dated and checked against it
        vers = {k: self.mem.record_ver(uid, k) for k in RECORD_KINDS} if uid else {}
        gen = self.mem.gen(uid) if uid else None  # wiped while the reply is written: nothing of this turn is kept
        hist = req.get("history") or []
        ask = lambda m, n=20: self.llm(m, max_tokens=n, temperature=0, timeout=8)[0]
        key = (uid or who.get("ip"), pid)
        plan = plan_reply(pid, hist, ask, self.recent_moves.get(key, []), self.mem.record_kinds(uid) if uid else ())
        users, astro, practical, vids, move = (plan[k] for k in ("users", "astro", "practical", "views", "move"))
        locate = lambda city: self.geo.find(city, lambda m: self.llm(m, max_tokens=80, temperature=0, timeout=10)[0])
        birth = dict(req["birth"]) if isinstance(req.get("birth"), dict) else {}
        # only what his last line said: once the page has saved it, an older line must not undo a later edit of the profile
        said = said_birth(hist, birth.get("slot"), birth.get("time"))[1]
        saved = {"time": said["time"], "slot": ""} if said.get("time") and said["time"] != birth.get("time") else {}
        saved.update({k: said[k] for k in ("date", "city") if said.get(k) and not birth.get(k)})
        if saved.get("city"):
            known = paipan.place(saved["city"])
            if known and not known["approx"]:
                saved["city"] = known["name"]  # "出生的，在杭州长大" -> 杭州
            elif not (known or locate(saved["city"])):
                del saved["city"]  # "出生在一个小县城"
        birth.update(saved)
        if birth.get("city") and astro:  # a 盘州 or a small town abroad: look it up once
            birth["geo"] = locate(birth["city"]) or birth.get("geo")
        if birth:
            req = dict(req, birth=birth)
        know = self.mem.context(uid, " ".join(users[-3:]), pid, brief=astro) if uid else None
        # his turn: the pictures he sent and the words he typed after them (several pictures and one line, one reply)
        turn = [str(h.get("content") or "") for h in itertools.takewhile(lambda h: h.get("role") == "user", reversed(hist))][::-1]
        if know and plan["records"]:  # his 饮食、睡眠、心情 records, when this reply needs them (records_needed)
            know = "\n".join([know] + [self.mem.record_block(uid, k, *(span or ())) for k, span in plan["records"].items()])
        if move:
            with self.lock:
                self.recent_moves[key] = [move] + self.recent_moves.get(key, [])[:1]
                if len(self.recent_moves) > 5000:  # in-process only; a restart just forgets which routes were used
                    self.recent_moves.clear()
        msgs = build_messages(pid, req, know, views_block(pid, vids), move, saved, practical)
        tokens, cap = reply_limits(pid, plan, self.mem.reply_cap(uid, pid) if uid else REPLY_MAX)
        raw, model, ms = self.llm(msgs, max_tokens=tokens, timeout=40)  # the page gives up at 45 s
        # A tester sent the same long message twice in one visit and got the same 587 字 back: with its own answer to that very
        # message right above, the model copied it. A reply that mostly repeats an earlier one is written once more.
        earlier = [str(h.get("content") or "") for h in hist if h.get("role") != "user"]
        if earlier and max(overlap(raw, e) for e in earlier) > COPY_LIMIT:
            retry = [dict(msgs[0], content=msgs[0]["content"] + "\n\n" + REWRITE_NOTE)] + msgs[1:]
            try:
                raw2, model2, ms2 = self.llm(retry, max_tokens=tokens, temperature=1.0, timeout=40)
                if max(overlap(raw2, e) for e in earlier) < max(overlap(raw, e) for e in earlier):
                    raw, model, ms = raw2, model2, ms + ms2
            except LLMError:
                pass  # the first reply still stands
        told = any("12356" in str(h.get("content") or "") for h in hist if h.get("role") != "user")
        reply = finish_reply(pid, plan, msgs, raw, cap, told)
        tid = self.log("chat", who, pid, req, user=msgs[-1]["content"], reply=reply, raw=raw if fit(raw, cap) != raw else None,
                       cap=cap, model=model, ms=ms, pv=prompt_version(pid), know=know, views=vids or None,
                       records={k: span and [d.isoformat() for d in span] for k, span in plan["records"].items()} or None,
                       move=practical or move,
                       hist=req.get("history"), profile=clip(req.get("profile"), 800) or None,
                       birth=req.get("birth") if isinstance(req.get("birth"), dict) else None, saved=saved or None)
        # one line per picture and one for his words: in a single 1000-字 line, long pictures pushed his words out
        lines = [("user", clip(t, 600 if t.startswith(IMAGE_MARK) else 1000)) for t in turn if not t.startswith(IMAGE_LOST)] or \
                [("user", clip(msgs[-1]["content"], 1000))]
        try:
            if uid and self.mem.append(uid, pid, lines + [("ai", reply)], gen) >= 8:
                self.fold_pool.submit(self.fold, uid, 8)
        except OSError as e:  # his profile is there but can't be read: the reply still goes back, nothing is written over it
            sys.stderr.write("append %s: %s\n" % (uid[:6], e))
        # his turn before this one (past the reply to it): a meal told there makes "其实只有半碗" a correction of it
        rest = itertools.dropwhile(lambda h: h.get("role") != "user", reversed(hist[:len(hist) - len(turn)]))
        before = [str(h.get("content") or "") for h in itertools.takewhile(lambda h: h.get("role") == "user", rest)]
        kinds = record_kinds_said(turn, reply, bool(move), lambda: self.after_meal(uid, before, at, vers["diet"])) if uid else []
        if kinds:  # 饮食、睡眠、心情 he gave in this turn -> his records, in the background
            self.queue_records(uid, functools.partial(self.note_records, uid, pid, kinds, turn, reply, tid, who, at, vers))
        out = {"reply": reply, "model": model, "ms": ms, "tid": tid}
        if saved:  # the page keeps it in his profile, so the next visit (and the other self) has it too
            out["birth"] = saved
        return out

    def see(self, req, who):
        """A picture he sent -> {seen: what is in it, as text}. The picture is kept next to the logs (see IMAGE_DIR)."""
        m = IMAGE_URL.fullmatch(str(req.get("image") or "").strip())
        if not m:
            raise ValueError("need a jpeg, png or webp data URL")
        raw = base64.b64decode(re.sub(r"\s", "", m.group(2)))
        if not 0 < len(raw) <= IMAGE_MAX_BYTES:
            raise ValueError("bad image size")
        if not self._bump("image_calls", 1, IMAGE_DAILY):
            raise DailyCap()
        url = "data:image/%s;base64,%s" % (m.group(1), base64.b64encode(raw).decode())
        ask = _read(SEE_PATH)
        if not ask:  # without it the model analyses the chart itself instead of copying it out
            raise LLMError("see-image prompt missing")
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": ask}]}]
        t0, text, err = time.time(), "", "no model"
        for (endpoint, model), wait in zip(SEE_MODELS, (30, 45)):  # deepseek reads one in ~4 s; page waits 85 s, nginx 90
            try:
                # 0.3, not 0: at 0 a meal photo sometimes came back as "一个白色小碗，里面是面条" seventy times; the 紫微
                # screenshot still copies out right at 0.3 (12 palaces, 3 runs of 3)
                text = strip_md(llm_call(endpoint, model, msgs, max_tokens=1600, temperature=0.3, timeout=wait, think=False))
                if looping(text):
                    again = strip_md(llm_call(endpoint, model, msgs, max_tokens=1600, temperature=0.6, timeout=wait, think=False))
                    text = again if again and not looping(again) else unloop(text)
            except LLMError as e:
                err = str(e)
            if text:
                break
            sys.stderr.write("%s failed (%s)\n" % (model, err or "empty reply"))
        if not text:
            raise LLMError(err)
        ms, pid = int((time.time() - t0) * 1000), str(req.get("p") or "")
        tid = self.log("image", who, pid if pid in PERSONAS else None, req, reply=text, model=model, ms=ms, bytes=len(raw))
        try:
            os.makedirs(IMAGE_DIR, exist_ok=True)
            with open(os.path.join(IMAGE_DIR, "%s-%s.%s" % (datetime.date.today().isoformat(), tid, m.group(1).replace("jpeg", "jpg"))), "wb") as f:
                f.write(raw)
        except OSError:
            pass  # the description still goes back
        return {"seen": fit(text, 1500), "model": model, "ms": ms, "tid": tid}

    def closing(self, pid, req, who):
        """One sentence that sums up the session, said when the user taps 结束本次对话."""
        msgs, said = build_closing(pid, req)
        gen = self.mem.gen(req["u"]) if valid_uid(req.get("u")) else None
        text, model, ms = self.llm(msgs, max_tokens=120, temperature=0.7, tidy=tidy_closing)
        text = postprocess("", text, pid=pid)  # the closing line never carries the hotline
        tid = self.log("closing", who, pid, req, user=said, reply=text, model=model, ms=ms, pv=prompt_version(pid))
        if valid_uid(req.get("u")):
            try:
                self.mem.append(req["u"], pid, [("closing", text)], gen)
                self.fold_pool.submit(self.fold, req["u"], 2)
            except OSError as e:  # as in chat: the line still goes back
                sys.stderr.write("append %s: %s\n" % (req["u"][:6], e))
        return {"text": text, "model": model, "ms": ms, "tid": tid}

    def opening(self, pid, req, who):
        """First line when he opens a chat after a while or after talking to the other self: what he said in the last
        hour (to either self) is picked up, anything older waits until he brings it up (see OPENING_FRESH)."""
        uid = req.get("u") if valid_uid(req.get("u")) else None
        f = None
        if uid and isinstance(req.get("follow"), str):  # he tapped a red dot: that thing, if it is still this card's
            try:
                f = self.mem.follow_take(uid, pid, req["follow"])
            except OSError as e:
                sys.stderr.write("follow_take %s: %s\n" % (uid[:6], e))
        just = [] if f else self.mem.just_said(uid) if uid else []
        s = self.mem.summary(uid) if uid else None
        know = "【你对他的了解】认识第 %d 天，他跟你说过 %d 句话。" % (s["days"], s["msgs"]) if s and s["msgs"] else None
        try:
            text, model, ms = self.llm(build_opening(pid, req, know, just, f), max_tokens=120)
        except Exception:
            if f:  # nothing was said about it: the dot stays, today isn't used
                self.mem.follow_undo(uid, f)
            raise
        text = postprocess("", drop_other_self(pid, fit(text, FOLLOW_MAX if f else 80), " ".join(just)))
        tid = self.log("open", who, pid, req, reply=text, model=model, ms=ms, pv=prompt_version(pid), know=know,
                       just=just or None, follow=f and f["id"], follow_text=f and f["text"])
        return {"text": text, "model": model, "ms": ms, "tid": tid, "follow": f and f["id"]}

    def portrait(self, req, who):
        """The portrait page's longer read of his sign and MBTI, and (once he has talked) of what he has said."""
        sign, mbti = str(req.get("sign") or ""), str(req.get("mbti") or "").upper()
        blood, animal = str(req.get("blood") or "").upper(), str(req.get("animal") or "")
        sign, mbti = (sign if sign in SIGNS else ""), (mbti if mbti in MBTI_TYPES else "")
        blood, animal = (blood if blood in BLOOD_TYPES else ""), (animal if len(animal) == 1 and animal in ANIMALS else "")
        if not (sign or mbti):
            raise ValueError("need a sign or an MBTI type")
        uid = req.get("u") if valid_uid(req.get("u")) else None
        heard = ""
        if uid:
            s = self.mem.summary(uid)
            heard = "\n".join(([s["portrait"]] if s["portrait"] else []) + ["- " + n["text"] for n in s["notes"][:15]])
        birth = req.get("birth") if isinstance(req.get("birth"), dict) else None
        if birth and birth.get("city"):
            geo = self.geo.find(birth["city"], lambda m: self.llm(m, max_tokens=80, temperature=0, timeout=10)[0])
            birth = dict(birth, geo=geo) if geo else birth
        facts = portrait_facts(sign, mbti, blood, animal, birth_chart(birth))
        with ThreadPoolExecutor(2) as pool:
            said = pool.submit(self.llm, build_portrait(facts, heard), 500, 0.8, 40) if heard else None
            for _ in range(2):  # now and then the model drops a section or renames its fields: ask once more
                raw, model, ms = self.llm(build_portrait(facts), max_tokens=2800, timeout=45)  # real ones take 5-20 s
                out = tidy_portrait(parse_json(raw) or {}, "")
                if (out["sign"] or not sign) and (out["mbti"] or not mbti) and out["mix"]:
                    break
            try:
                out["heard"] = tidy_portrait({}, said.result()[0])["heard"] if said else ""
            except LLMError:
                pass  # the sign/MBTI read still stands without it
        if not (out["sign"] or out["mbti"]):
            raise LLMError("portrait: nothing usable in the answer")
        self.log("portrait", who, "", req, sign=sign, mbti=mbti, reply=json.dumps(out, ensure_ascii=False), model=model, ms=ms)
        return out

    def fold(self, uid, min_user, wait=0):
        try:
            return self.mem.consolidate(uid, min_user, wait)
        except Exception as e:  # background work must never take the server down
            sys.stderr.write("consolidate %s failed: %s\n" % (uid[:6], e))
            return None

    def note_said(self, uid, pid, said, reply, tid, who):
        """His words the page answered itself (/api/event local): into what the selves know and his records, the same way
        as a chat turn (review 2026-11-27: "我只想说说，面试过了" got 导师's fixed reply, never reached the fold, and the
        面试 dot stayed). Not twice: /api/chat may have kept it already when the page gave up waiting for the reply."""
        try:
            if said not in self.mem.just_said(uid, minutes=3):
                if self.mem.append(uid, pid, [("user", clip(said, 1000)), ("ai", reply)]) >= 8:
                    self.fold_pool.submit(self.fold, uid, 8)
        except OSError as e:  # his profile is there but can't be read: nothing is written over it
            sys.stderr.write("append %s: %s\n" % (uid[:6], e))
        at, vers = datetime.datetime.now(), {k: self.mem.record_ver(uid, k) for k in RECORD_KINDS}
        kinds = record_kinds_said([said], reply, False, lambda: self.after_meal(uid, (), at, vers["diet"]))
        if kinds:
            self.queue_records(uid, functools.partial(self.note_records, uid, pid, kinds, [said], reply, tid, who, at, vers))

    def day_meals(self, uid, got, rec, at, ver):
        """For same_meal: all his meals of the day this correction is about (the day he said, or today), told before
        `at`: the model is shown 8 at most (review 2026-11-26: yesterday's 午饭 was the 9th back and came in again)."""
        if not got.get("same"):
            return []
        upto = at.isoformat(timespec="seconds")
        try:
            return self.mem.record_said(uid, "diet", ver, lambda r: r.get("d") == rec["d"] and str(r.get("t") or "") <= upto)
        except OSError:
            return []

    def after_meal(self, uid, before, at, ver):
        """Has he just told a meal: in his lines before this turn (its record may still be on the way), or one kept in
        the last 3 hours (to either self). For record_kinds_said: "其实只有半碗" alone is about that meal."""
        if any(DIET_SAID.search(t) or DIET_CALC.search(t) for t in before):
            return True
        try:
            return bool(self.mem.record_recent(uid, "diet", 3, at, ver))
        except OSError:
            return False

    def queue_records(self, uid, job):
        """His record jobs run one at a time, in the order he sent the turns (review 2026-11-25: two at once each found
        no mood for the day and both added one; and the later turn's state must be the day's last)."""
        with self.rec_lock:
            jobs = self.rec_jobs.setdefault(uid, deque())
            jobs.append(job)
            if len(jobs) > 1:
                return  # the one running drains it
        self.fold_pool.submit(self._run_records, uid)

    def _run_records(self, uid):
        while True:
            with self.rec_lock:
                job = self.rec_jobs[uid][0]
            job()  # never raises (note_records catches everything)
            with self.rec_lock:
                jobs = self.rec_jobs[uid]
                jobs.popleft()
                if not jobs:
                    del self.rec_jobs[uid]
                    return

    def note_records(self, uid, pid, kinds, turn, reply, tid, who, at, vers):
        """Background, after a reply: the 饮食、睡眠、心情 he gave in this turn -> his records, in one model call
        (RECORD_ASK). A follow-up about the same meal updates it; sleep and mood keep one record a day. at = when he sent
        the turn; vers = memory.record_ver then: nothing is written into a kind he cleared or deleted from since, and a
        kind he cleared (or everything, wiped) isn't even asked about: what he said goes to no model after that."""
        try:
            kinds = [k for k in kinds if self.mem.record_fresh(uid, k, vers[k])]
            if not kinds:
                return
            try:
                recent = self.mem.record_recent(uid, "diet", 3, at, vers["diet"], n=8, days=2) if "diet" in kinds else []
            except OSError:
                recent = []  # diet.json can't be read: the meal won't be written (record_put), the other kinds still are
            ask = RECORD_TURN % "、".join('"%s"' % k for k in kinds) + "\n".join(RECORD_ASK[k] for k in kinds)
            if recent:
                ask += RECORD_DIET_SAME % "\n".join(recent_meal_line(i + 1, m, at.date()) for i, m in enumerate(recent))
            if "diet" in kinds:
                ask += RECORD_DIET_FIX
            used = set()
            said = "\n".join(clip(t, 800) for t in turn)
            text = self.llm([{"role": "system", "content": ask},
                             {"role": "user", "content": "他发来的：\n%s\n\n回复：\n%s" % (said, reply)}],
                            max_tokens=900, temperature=0, timeout=30)[0]
            got = parse_json(text) or {}
            if len(kinds) == 1 and got and not set(got) & set(RECORD_KINDS):  # asked for one, it left out {"sleep": ...}
                got = {kinds[0]: got}
            for kind in kinds:
                found = got.get(kind)
                for one in (found if isinstance(found, list) else [found]):  # diet: a list, one meal each
                    rec = record_entry(kind, one, pid, tid, turn, at)
                    if not rec:  # kept in the archive too, to see what the extraction misses
                        self.log("record", who, pid, {"u": uid}, record=kind, kept=False, ref=tid,
                                 raw=clip(json.dumps(one, ensure_ascii=False) if kind in got else text, 300))
                        continue
                    pick, fix = same_meal(one, rec, recent, used, self.day_meals(uid, one, rec, at, vers["diet"])) \
                        if kind == "diet" else (None, False)
                    same = pick and pick["id"]
                    if same:
                        used.add(same)
                    elif fix:  # he is correcting a meal, but which one can't be told: kept, out of the sums, the rest as it was
                        rec["unmatched"] = True
                    try:
                        put = self.mem.record_put(uid, kind, rec, vers[kind], same)
                        why = (None if not fix or same else "unmatched") if put else "cleared"  # or deleted: kind, meal, day
                    except OSError as e:  # the file is there but can't be read: left as it is
                        put, why = None, "unreadable"
                        sys.stderr.write("record_put %s %s: %s\n" % (uid[:6], kind, e))
                    self.log("record", who, pid, {"u": uid}, record=kind, kept=bool(put), ref=tid, same=bool(same) or None,
                             reply=json.dumps(put or rec, ensure_ascii=False), why=why)
        except Exception as e:  # background work must never take the server down
            sys.stderr.write("records %s failed: %s\n" % (uid[:6], e))

    # -- the archive: everything said, one jsonl per day (read with tools/chatlog.py)
    def log(self, kind, who, pid, req, **fields):
        """who = {ip, ua} of the request. Returns the record's id (tid), which the page sends back with 👍/👎."""
        tid = uuid.uuid4().hex[:12]
        rec = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "kind": kind, "tid": tid,
               "u": req.get("u") if valid_uid(req.get("u")) else None, "p": pid, "nick": clip(req.get("nick"), 12) or None,
               **who, **{k: v for k, v in fields.items() if v not in (None, "")}}
        path = os.path.join(LOG_DIR, datetime.date.today().isoformat() + ".jsonl")
        with self.disk_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return tid

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
        for tries in range(4):  # a long reply is 6-12 chunks at once; some voices refuse that many ("AccessLimit")
            try:
                data = self.tts.synth(text, voice, speed, "mp3", rate, emotion)
                break
            except TTSError as e:
                if e.code not in ("LimitExceeded.AccessLimit", "RequestLimitExceeded") or tries == 3:
                    raise
                time.sleep(0.6 * (tries + 1) + random.random() * 0.6)
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

    def who(self):
        return {"ip": self.client_ip(), "ua": clip(self.headers.get("User-Agent"), 200)}

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
            if self.limited("fb"):
                return None
            try:  # one that can't be read is an error, not "nothing known yet": the page keeps what it showed
                return self.send_json(200, APP.mem.summary(q["u"], strict=True))
            except OSError as e:
                sys.stderr.write("summary %s: %s\n" % (q["u"][:6], e))
                return self.send_json(500, {"error": "could not read"})
        if path == "/api/records":  # his 饮食、睡眠、心情 records (the page's 我的记录)
            if not valid_uid(q.get("u")) or q.get("kind") not in RECORD_KINDS:
                return self.send_json(400, {"error": "bad request"})
            if self.limited("fb"):
                return None
            try:  # the page says 暂时取不到, not 还没有记录
                return self.send_json(200, {"kind": q["kind"], "items": APP.mem.records(q["u"], q["kind"])})
            except OSError as e:
                sys.stderr.write("records %s: %s\n" % (q["u"][:6], e))
                return self.send_json(500, {"error": "could not read"})
        if path == "/api/persona":
            pid = q.get("p", "")
            if pid not in PERSONAS:
                return self.send_json(400, {"error": "unknown persona"})
            return self.send_json(200, {"p": pid, "model": CHAT_PRIMARY[1],
                                        "text": _read(os.path.join(PERSONA_DIR, pid, "人格档案.md"))})
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        handler = {"/api/chat": self.handle_llm, "/api/closing": self.handle_llm, "/api/open": self.handle_llm,
                   "/api/sync": self.handle_self, "/api/card": self.handle_self, "/api/self/forget": self.handle_self,
                   "/api/self/wipe": self.handle_self, "/api/self/import": self.handle_self,
                   "/api/records/delete": self.handle_self, "/api/records/clear": self.handle_self,
                   "/api/fb": self.handle_fb, "/api/event": self.handle_fb, "/api/asr": self.handle_asr,
                   "/api/tts": self.handle_tts, "/api/portrait": self.handle_portrait,
                   "/api/chart": self.handle_chart, "/api/image": self.handle_image}.get(path)
        if not handler:
            return self.send_json(404, {"error": "not found"})
        return handler(path)

    def handle_image(self, path):
        """{u, p, image: data URL} -> {seen}: what is in the picture, for the page to send on as his message."""
        try:
            req = self.read_json(IMAGE_MAX_BYTES * 4 // 3 + 4000)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if self.limited("image"):
            return None
        try:
            return self.send_json(200, APP.see(req, self.who()))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except LLMError as e:
            APP.log("error", self.who(), None, req, path=path, error=str(e)[:300])
            return self.llm_failed(e)

    def handle_chart(self, path):
        """排盘 for the portrait page: arithmetic, plus one short model call the first time a birth place isn't in the table."""
        try:
            req = self.read_json(2000)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if self.limited("fb"):
            return None
        if isinstance(req, dict) and req.get("city"):
            try:
                geo = APP.geo.find(req["city"], lambda m: APP.llm(m, max_tokens=80, temperature=0, timeout=10)[0])
            except LLMError:
                geo = None
            if geo:
                req = dict(req, geo=geo)
        chart = birth_chart(req)
        return self.send_json(200, chart) if chart else self.send_json(400, {"error": "need a birth date"})

    def handle_portrait(self, path):
        try:
            req = self.read_json(4000)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if self.limited("chat"):
            return None
        try:
            return self.send_json(200, APP.portrait(req, self.who()))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except LLMError as e:
            return self.llm_failed(e)

    def handle_llm(self, path):
        """/api/chat, /api/closing, /api/open: {p, u, nick, ...} -> the persona's words."""
        try:
            req = self.read_json(48000)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if self.limited("chat"):
            return None
        pid = str(req.get("p", ""))
        if pid not in PERSONAS:
            return self.send_json(400, {"error": "unknown persona"})
        run = {"/api/chat": APP.chat, "/api/closing": APP.closing, "/api/open": APP.opening}[path]
        try:
            return self.send_json(200, run(pid, req, self.who()))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except LLMError as e:
            last = next((h.get("content") for h in reversed(req.get("history") or []) if h.get("role") == "user"), None)
            APP.log("error", self.who(), pid, req, path=path, error=str(e)[:300], user=clip(last, 2000))
            return self.llm_failed(e)

    def handle_self(self, path):
        """Long-term memory: /api/sync folds the latest talk in (called when the user leaves a chat); /api/card notes today's card;
        forget / wipe let the user delete what the selves know; import seeds from older local notes."""
        try:
            req = self.read_json(8192)
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        uid = req.get("u")
        if not valid_uid(uid):
            return self.send_json(400, {"error": "bad user id"})
        # /api/sync fires on every switch between the selves; on the chat budget it starved his real messages (429 ->
        # scripted reply). It only spends a model call when there are new lines, and those already passed /api/chat.
        if self.limited("fb"):
            return None
        try:
            return self.self_op(path, uid, req)
        except OSError as e:  # a file of his can't be read or written (memory.Unreadable): left as it is, and said so
            sys.stderr.write("%s %s: %s\n" % (path, uid[:6], e))
            return self.send_json(500, {"error": "storage"})

    def self_op(self, path, uid, req):
        if path == "/api/sync":
            # one line is enough ("后天去拿体检结果" and gone); his turn being answered and a fold already running are
            # waited for, so the page's refresh after this sees it all folded; busy: not done in time, the page asks again
            r = APP.fold(uid, 1, wait=SYNC_WAIT)
            if r is BUSY:
                return self.send_json(200, {"memories": [], "episode": "", "busy": True})
            r = r or {"add": [], "episode": ""}
            return self.send_json(200, {"memories": r["add"], "episode": r["episode"]})
        if path == "/api/card":  # today's 彩虹卡 is read aloud on the page, no reply; the selves just remember it
            card = next((c for c in load_cards()["cards"] if c["id"] == str(req.get("id", ""))), None)
            pid = str(req.get("p", ""))
            if not card or pid not in PERSONAS:
                return self.send_json(400, {"error": "unknown card"})
            APP.mem.append(uid, pid, [("card", "抽到今天的彩虹卡「%s」" % card["t"])])
            APP.log("card", self.who(), pid, req, card=card["id"], reply=card["t"])
            return self.send_json(200, {"ok": True})
        if path == "/api/self/forget":
            return self.send_json(200, {"ok": APP.mem.forget(uid, str(req.get("id", "")))})
        if path == "/api/self/wipe":
            left = APP.mem.wipe(uid)
            if left:  # the page says what is still there, and that he can try again
                return self.send_json(500, {"error": "could not delete everything", "left": left})
            return self.send_json(200, {"ok": True})
        if path.startswith("/api/records/"):  # 我的记录: delete one, or clear a kind
            kind = req.get("kind")
            if kind not in RECORD_KINDS:
                return self.send_json(400, {"error": "unknown record kind"})
            if path == "/api/records/clear":
                if not APP.mem.record_clear(uid, kind):
                    return self.send_json(500, {"error": "could not clear"})
                return self.send_json(200, {"ok": True})
            return self.send_json(200, {"ok": APP.mem.record_delete(uid, kind, str(req.get("id", "")))})  # OSError: 500
        notes = req.get("notes") if isinstance(req.get("notes"), list) else []
        return self.send_json(200, {"imported": APP.mem.import_notes(uid, notes)})

    def handle_fb(self, path):
        """/api/fb {p, u, tid, mid, v: 1|-1|0, reply, user}: 👍/👎 on one reply (ref = the tid of the record it rates).
        /api/event {p, u, kind, ...}: what the page said or saw without the model, so the archive has whole conversations:
          local  {why: crisis|fixed|offline|llm-failed|nudge, user, reply}   scripted replies
          rating {rating: 1-5, note, msgs}                                    the sheet after 结束本次对话"""
        try:
            req = self.read_json(16384)
            pid = str(req.get("p", ""))
            if path == "/api/fb":
                v = int(req.get("v", 0))
                kind, ok = "fb", v in (-1, 0, 1)
                fields = {"ref": clip(req.get("tid"), 20), "mid": clip(req.get("mid"), 40), "v": v,
                          "reply": clip(req.get("reply"), 1000), "user": clip(req.get("user"), 500)}
            else:
                kind = str(req.get("kind", ""))
                ok = kind in ("local", "rating")
                fields = {"why": clip(req.get("why"), 20), "user": clip(req.get("user"), 1000), "reply": clip(req.get("reply"), 2000),
                          "rating": int(req.get("rating") or 0) or None, "note": clip(req.get("note"), 500),
                          "msgs": int(req.get("msgs") or 0) or None}
        except (ValueError, TypeError):
            return self.send_json(400, {"error": "bad request"})
        if pid not in PERSONAS or not ok:
            return self.send_json(400, {"error": "bad request"})
        if self.limited("fb"):
            return None
        tid = APP.log(kind, self.who(), pid, req, **fields)
        if kind == "local" and fields["why"] in LOCAL_RECORDED and valid_uid(req.get("u")) and fields["user"]:
            APP.note_said(req["u"], pid, fields["user"], fields["reply"] or "", tid, self.who())
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
        ms = int((time.time() - t0) * 1000)
        q = dict(urllib.parse.parse_qsl(self.path.partition("?")[2]))
        APP.log("asr", self.who(), q.get("p") if q.get("p") in PERSONAS else None, q, user=text, secs=round(secs, 1), ms=ms)
        return self.send_json(200, {"text": text, "secs": round(secs, 1), "ms": ms})

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
