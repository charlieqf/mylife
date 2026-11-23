"""排盘（server/paipan.py）对照公开数据：Meeus 的算例、春节日期、闰月、立春时刻、已知八字、日出时上升 = 太阳、
真太阳时正午天顶 = 太阳、行星换座、城市识别。改了排盘就跑一遍（仓库根目录）：python tools/check_paipan.py"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import paipan as P  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

bad = 0
def ok(cond, what, got=""):
    global bad
    bad += not cond
    print(("ok  " if cond else "FAIL") + " " + what + ("  -> %s" % (got,) if got != "" else ""))

# Meeus worked examples
ok(abs(P.moon_lon(2448724.5) - 133.167265) < 0.005, "Meeus 47.a moon λ 133.167265", P.moon_lon(2448724.5))
ok(abs(P.sun_lon(2448908.5) - 199.90895) < 0.01, "Meeus 25.a sun λ 199.90895", P.sun_lon(2448908.5))
ok(abs(P.new_moon(-283) - 2443192.65118) < 0.0005, "Meeus 49.a new moon 2443192.65118", P.new_moon(-283))
ok(abs(P.planet_lon("金星", 2448976.5) - 313.08102) < 0.05, "Meeus 33.a Venus λ 313.08102", P.planet_lon("金星", 2448976.5))

# day pillar
ok(P.gz(P.day_index(datetime.date(1949, 10, 1))) == "甲子", "1949-10-01 甲子日")
ok(P.gz(P.day_index(datetime.date(2000, 1, 1))) == "戊午", "2000-01-01 戊午日")

# 立春 (Beijing time)
for y, want in [(2023, "02-04 10:42"), (2024, "02-04 16:27"), (2025, "02-03 22:10"), (2026, "02-04 04:02")]:
    j = P.term_jde(315, P.J2000 + (y - 2000) * 365.2422 + 34)
    got = P.utc_of(j - (P.tt(j) - j)) + P.BJ
    w = datetime.datetime.strptime("%d-%s" % (y, want), "%Y-%m-%d %H:%M")
    ok(abs((got - w).total_seconds()) < 600, "立春 %d %s" % (y, want), got.strftime("%m-%d %H:%M"))

# Chinese New Year and leap months
CNY = {1950: "02-17", 1960: "01-28", 1970: "02-06", 1980: "02-16", 1984: "02-02", 1985: "02-20", 1986: "02-09",
       1987: "01-29", 1988: "02-17", 1989: "02-06", 1990: "01-27", 1991: "02-15", 1992: "02-04", 1993: "01-23",
       1994: "02-10", 1995: "01-31", 1996: "02-19", 1997: "02-07", 1998: "01-28", 1999: "02-16", 2000: "02-05",
       2001: "01-24", 2002: "02-12", 2003: "02-01", 2004: "01-22", 2005: "02-09", 2006: "01-29", 2007: "02-18",
       2008: "02-07", 2009: "01-26", 2010: "02-14", 2011: "02-03", 2012: "01-23", 2013: "02-10", 2014: "01-31",
       2015: "02-19", 2016: "02-08", 2017: "01-28", 2018: "02-16", 2019: "02-05", 2020: "01-25", 2021: "02-12",
       2022: "02-01", 2023: "01-22", 2024: "02-10", 2025: "01-29", 2026: "02-17"}
miss = []
for y, md in CNY.items():
    d = datetime.date.fromisoformat("%d-%s" % (y, md))
    a, b = P.lunar(d), P.lunar(d - datetime.timedelta(days=1))
    if not (a[1:] == (1, 1, False) and a[0] == y and b[1] == 12):
        miss.append((y, a, b))
ok(not miss, "春节 %d 年" % len(CNY), miss)
LEAP = {1952: 5, 1955: 3, 1957: 8, 1960: 6, 1963: 4, 1966: 3, 1968: 7, 1971: 5, 1974: 4, 1976: 8, 1979: 6, 1982: 4,
        1984: 10, 1987: 6, 1990: 5, 1993: 3, 1995: 8, 1998: 5, 2001: 4, 2004: 2, 2006: 7, 2009: 5, 2012: 4, 2014: 9,
        2017: 6, 2020: 4, 2023: 2, 2025: 6, 2028: 5, 2033: 11}
miss = []
for y, m in LEAP.items():
    found = set()
    d = datetime.date(y, 1, 1)
    while d.year == y or (y == 2033 and d < datetime.date(2034, 2, 1)):
        ly, lm, ld, lp = P.lunar(d)
        if lp and ly == y:
            found.add(lm)
        d += datetime.timedelta(days=1)
    if found != {m}:
        miss.append((y, m, found))
ok(not miss, "闰月 %d 年" % len(LEAP), miss)
ok(P.lunar_text(*P.lunar(datetime.date(1990, 8, 5))) == "庚午年六月十五", "1990-08-05 农历", P.lunar_text(*P.lunar(datetime.date(1990, 8, 5))))

# 八字
c = P.chart("2008-08-08", "20:00", "", "北京")
ok(c["bazi"]["pillars"] == ["戊子", "庚申", "庚辰", "丙戌"], "2008-08-08 20:00 北京 戊子 庚申 庚辰 丙戌", c["bazi"]["pillars"])
for t, want in [("16:00", ["癸卯", "乙丑"]), ("17:00", ["甲辰", "丙寅"])]:
    c = P.chart("2024-02-04", t, "", "北京")
    ok(c["bazi"]["pillars"][:2] == want, "2024-02-04 %s 立春前后" % t, c["bazi"]["pillars"])
c = P.chart("1990-08-05", "10:30", "", "成都")
ok(any("夏令时" in n for n in c["notes"]), "1990 夏令时提示")

# angles: at geometric sunrise the Sun is on the ascendant, at apparent noon on the MC
for city, date, hm in [("北京", "2024-06-21", "04:50"), ("悉尼", "2024-12-21", "05:45"), ("伦敦", "2024-03-20", "06:05")]:
    pl = P.place(city)
    utc, _ = P._to_utc(datetime.datetime.fromisoformat(date + " " + hm), pl["tz"])
    jd = P.jd_ut(utc)
    asc = P.angles(jd, pl["lat"], pl["lon"])[3]
    sun = P.sun_lon(P.tt(jd))
    ok(abs((asc - sun + 180) % 360 - 180) < 2.5, "日出时上升≈太阳 %s %s" % (city, date), "asc %.1f sun %.1f" % (asc, sun))
for city in ("北京", "悉尼"):
    pl = P.place(city)
    utc = datetime.datetime(2024, 5, 10, 0)
    for _ in range(3):  # find apparent noon
        st = P.solar_time(utc, pl["lon"])
        utc -= (st - st.replace(hour=12, minute=0, second=0, microsecond=0))
    jd = P.jd_ut(utc)
    mc = P.angles(jd, pl["lat"], pl["lon"])[2]
    ok(abs((mc - P.sun_lon(P.tt(jd)) + 180) % 360 - 180) < 0.3, "真太阳时正午 天顶≈太阳 %s" % city, mc)

# planets: signs on known dates
def sign_on(body, date):
    jde = P.tt(P.jd_ut(datetime.datetime.fromisoformat(date + " 00:00")))
    return P.sign_deg(P._lon(body, jde))
ok(sign_on("木星", "2024-06-15")[0] == "双子", "木星 2024-06 双子", sign_on("木星", "2024-06-15"))
ok(sign_on("土星", "2024-06-15")[0] == "双鱼", "土星 2024-06 双鱼", sign_on("土星", "2024-06-15"))
ok(sign_on("火星", "2025-01-15")[0] == "巨蟹", "火星 2025-01 巨蟹(逆行)", sign_on("火星", "2025-01-15"))
c = P.chart("2024-04-10", "12:00", "", "北京")
ok(c["west"]["bodies"][2]["retro"], "水星 2024-04-10 逆行")
c = P.chart("2024-06-10", "12:00", "", "北京")
ok(not c["west"]["bodies"][2]["retro"], "水星 2024-06-10 顺行")

# place
for s, want in [("浙江杭州", "杭州"), ("成都市", "成都"), ("Sydney", "悉尼"), ("吉林市", "吉林市"), ("浙江湖州长兴", "湖州"),
                ("四川凉山", "成都"), ("火星", None)]:
    pl = P.place(s)
    ok((pl or {}).get("name") == want, "城市 %s -> %s" % (s, want), pl)

print("\nFAILS:", bad)
sys.exit(1 if bad else 0)
