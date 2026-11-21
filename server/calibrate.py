#!/usr/bin/env python3
"""Measure each candidate Tencent voice: does it work on this account, its default
characters/second at Speed=0, and the actual rate when we ask for TARGET cps.

Run on the server with the secrets in the environment:
  set -a; . /etc/mylife/secrets.env; set +a; python3 calibrate.py [voice ids...] > calib.json
"""
import io
import json
import os
import sys
import wave
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mylife_server import TencentTTS, TTSError, count_chars, speed_for_cps  # noqa: E402

# ~100 spoken characters of conversational text (design doc 5.6).
TEXT = ("其实我一直在想你上次说的那件事。你说你不在乎，可真不在乎的人，不会把它讲三遍。"
        "我们先不急着下结论，慢慢说。今晚你可以先不想明天，把心里那句话说出来就好，说出来，它就没那么重了。")
TARGET = 5.6

CANDIDATES = {
    # male
    402002: "云大围 磁性男声", 403010: "云嘉和 温和男声", 403009: "云嘉尔 慵懒男声",
    603006: "沉稳青叔 聊天男声", 603005: "知心大林 聊天男声", 603003: "随和老李 聊天男声",
    602004: "暖心阿灿 聊天男声", 502006: "智小悟 聊天男声", 601008: "爱小豪 多情感男声",
    501005: "飞镜 聊天男声", 501006: "千嶂 聊天男声", 601011: "爱小川 聊天男声",
    # female
    403014: "云佳洁 轻熟姐姐", 402000: "云晓芙 聊天女声", 402001: "云小团 温柔甜美女声",
    403011: "云嘉瑜 情感解说女声", 603004: "温柔小柠 聊天女声", 602005: "专业梓欣 聊天女声",
    502001: "智小柔 聊天女声", 602003: "爱小悠 聊天女声", 601009: "爱小芊 多情感女声",
    601010: "爱小娇 多情感女声", 501004: "月华 聊天女声",
}


def wav_seconds(data, rate):
    try:
        with wave.open(io.BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate())
    except wave.Error:
        return max(0, len(data) - 44) / (rate * 2.0)


CURVE_SPEEDS = [-1, 0, 1, 2, 3]


def measure(tts, vid, n):
    row = {"id": vid, "label": CANDIDATES.get(vid, "")}
    try:
        curve = []
        for sp in CURVE_SPEEDS:
            d = wav_seconds(tts.synth(TEXT, vid, sp, "wav", 16000), 16000)
            curve.append([sp, round(n / d, 3)])
        row.update(ok=True, baseCps=dict(curve)[0], curve=curve)
        sp = speed_for_cps(curve, TARGET)
        d = wav_seconds(tts.synth(TEXT, vid, sp, "wav", 16000), 16000)
        row.update(speedFor56=sp, actualAt56=round(n / d, 2))
    except TTSError as e:
        row.update(ok=False, error=e.code, message=e.message[:160])
    print(json.dumps(row, ensure_ascii=False), file=sys.stderr)
    return row


def main():
    tts = TencentTTS(os.environ["TENCENT_SECRET_ID"], os.environ["TENCENT_SECRET_KEY"],
                     os.environ.get("TENCENT_TTS_REGION", "ap-shanghai"))
    ids = [int(a) for a in sys.argv[1:]] or list(CANDIDATES)
    n = count_chars(TEXT)
    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(lambda v: measure(tts, v, n), ids))
    print(json.dumps({"chars": n, "target": TARGET, "results": out}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
