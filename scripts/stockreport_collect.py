#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pomiai 데일리 주식 리포트 수집기
- GitHub Actions에서 주기 실행되어 data/stockreport/latest.json 을 갱신한다.
- 소스마다 따로 try/except 로 감싸고 성공 여부를 sources 에 남긴다.
  한 곳이 막혀도 나머지는 채워지고, 페이지는 어느 소스가 실패했는지 그대로 보여준다.
- 공개 페이지용: 원본 순위표를 그대로 싣지 않고 가공한 신호(동시 순매수, 이격도 단계 등)를 만든다.
"""
import json
import os
import re
import sys
import time
import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

KST = dt.timezone(dt.timedelta(hours=9))
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "stockreport")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://finance.naver.com/",
}

# 관심종목 (이름은 표시용, 코드가 기준). 수집 시 네이버 종목명과 대조해 불일치를 표시한다.
WATCHLIST = [
    ("위메이드맥스", "101730"), ("삼성전자우", "005935"), ("LG우", "003555"),
    ("LG디스플레이", "034220"), ("한화", "000880"), ("롯데쇼핑", "023530"),
    ("GS리테일", "007070"), ("한화엔진", "082740"), ("HD건설기계", "267270"),
    ("POSCO홀딩스", "005490"), ("LG화학우", "051915"), ("대한항공", "003490"),
    ("두산밥캣", "241560"), ("한국금융지주", "071050"), ("삼성생명", "032830"),
    ("삼성물산", "028260"), ("한국전력", "015760"), ("에코프로", "086520"),
]

# 이격도(종가 ÷ 120일선 × 100) 구간별 매수 비중 단계. 경계값은 여기서 바꾼다.
DISPARITY_STAGES = [(100, 0), (95, 1), (90, 2), (85, 3)]  # (하한, 단계). 85 미만은 4단계

SOURCES = {}


def now_kst():
    return dt.datetime.now(KST)


def get(url, encoding=None, timeout=12, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    if encoding:
        r.encoding = encoding
    return r


def num(s):
    """'1,234.5', '+3.2%', '-1,200' 같은 문자열을 float 으로. 실패하면 None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[,%\s원억주]", "", str(s)).replace("▲", "").replace("▼", "-").replace("−", "-")
    if t in ("", "-", "+"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def run(name, fn, default=None):
    """소스 하나를 실행하고 성공/실패를 기록한다."""
    t0 = time.time()
    try:
        out = fn()
        empty = out is None or out == [] or out == {}
        SOURCES[name] = {"ok": not empty, "msg": "빈 응답" if empty else "", "ms": int((time.time() - t0) * 1000)}
        return default if empty else out
    except Exception as e:  # noqa: BLE001 - 소스별 실패는 기록하고 계속 간다
        SOURCES[name] = {"ok": False, "msg": f"{type(e).__name__}: {str(e)[:140]}", "ms": int((time.time() - t0) * 1000)}
        return default


# ---------------------------------------------------------------- 지수
def fetch_index(code):
    """네이버 모바일 API → 실패 시 PC 페이지 파싱."""
    try:
        j = get(f"https://m.stock.naver.com/api/index/{code}/basic").json()
        val = num(j.get("closePrice"))
        if val:
            chg = num(j.get("compareToPreviousClosePrice"))
            pct = num(j.get("fluctuationsRatio"))
            return {"value": val, "chg": chg, "pct": pct,
                    "at": j.get("localTradedAt") or "", "status": j.get("marketStatus") or ""}
    except Exception:  # noqa: BLE001
        pass
    html = get(f"https://finance.naver.com/sise/sise_index.naver?code={code}", encoding="euc-kr").text
    soup = BeautifulSoup(html, "html.parser")
    val = num(soup.select_one("#now_value").get_text())
    raw = soup.select_one("#change_value_and_rate").get_text(" ", strip=True)
    m = re.search(r"([\d,.]+)\s*([+-]?[\d.]+)%", raw)
    chg = num(m.group(1)) if m else None
    pct = num(m.group(2)) if m else None
    if pct is not None and pct < 0 and chg is not None:
        chg = -abs(chg)
    return {"value": val, "chg": chg, "pct": pct, "at": "", "status": ""}


def fetch_index_flow(code):
    """지수의 투자자별 순매수(억원). 장중에는 잠정치."""
    j = get(f"https://m.stock.naver.com/api/index/{code}/trend").json()
    out = {"personal": num(j.get("personalValue")), "foreign": num(j.get("foreignValue")),
           "inst": num(j.get("institutionalValue")), "date": j.get("bizdate") or ""}
    return out if out["foreign"] is not None else None


# ---------------------------------------------------------------- 일별 시세
def fetch_daily(code, days=260):
    end = now_kst().strftime("%Y%m%d")
    start = (now_kst() - dt.timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
    url = ("https://fchart.stock.naver.com/siseJson.naver?symbol=%s&requestType=1"
           "&startTime=%s&endTime=%s&timeframe=day" % (code, start, end))
    return parse_sisejson(get(url).text)


def parse_sisejson(text):
    rows = []
    for m in re.finditer(r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', text):
        rows.append({"d": m.group(1), "o": float(m.group(2)), "h": float(m.group(3)),
                     "l": float(m.group(4)), "c": float(m.group(5)), "v": float(m.group(6))})
    return rows


def ma(rows, n):
    if len(rows) < n:
        return None
    return sum(r["c"] for r in rows[-n:]) / n


def stage_of(disp):
    if disp is None:
        return None
    for floor, stage in DISPARITY_STAGES:
        if disp >= floor:
            return stage
    return 4


def fetch_stock_name(code):
    try:
        j = get(f"https://m.stock.naver.com/api/stock/{code}/basic").json()
        return j.get("stockName") or ""
    except Exception:  # noqa: BLE001
        return ""


def build_watch():
    out = []
    for name, code in WATCHLIST:
        item = {"name": name, "code": code}
        try:
            rows = fetch_daily(code)
            if len(rows) >= 2:
                last, prev = rows[-1], rows[-2]
                m120 = ma(rows, 120)
                disp = (last["c"] / m120 * 100) if m120 else None
                item.update({
                    "date": last["d"], "close": last["c"],
                    "pct": round((last["c"] / prev["c"] - 1) * 100, 2) if prev["c"] else None,
                    "ma20": round(ma(rows, 20), 1) if ma(rows, 20) else None,
                    "ma60": round(ma(rows, 60), 1) if ma(rows, 60) else None,
                    "ma120": round(m120, 1) if m120 else None,
                    "disp": round(disp, 1) if disp else None,
                    "stage": stage_of(disp),
                    "spark": [r["c"] for r in rows[-60:]],
                })
            real = fetch_stock_name(code)
            if real and real.replace(" ", "") != name.replace(" ", ""):
                item["name_check"] = real  # 코드와 이름이 다르면 페이지에 경고 표시
        except Exception as e:  # noqa: BLE001
            item["err"] = f"{type(e).__name__}"
        out.append(item)
        time.sleep(0.15)
    ok = sum(1 for i in out if "close" in i)
    if ok == 0:
        raise RuntimeError("관심종목 시세를 하나도 받지 못함")
    return out


# ---------------------------------------------------------------- 종목별 수급
UNIVERSE = (("KOSPI", "코스피", 60), ("KOSDAQ", "코스닥", 40))


def fetch_universe():
    out = []
    for cat, mkt, n in UNIVERSE:
        j = get(f"https://m.stock.naver.com/api/stocks/marketValue/{cat}?page=1&pageSize={n}").json()
        for st in j.get("stocks", []):
            if st.get("stockEndType") != "stock":
                continue
            out.append({"name": st.get("stockName"), "code": st.get("itemCode"), "market": mkt})
    return out


def fetch_stock_trend(code):
    j = get(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=3").json()
    return j if isinstance(j, list) else []


def build_supply():
    """시총 상위 100종목의 최근 영업일 외국인·기관 순매수 금액을 계산해 순위를 매긴다.
    종목별 투자자 매매는 장 마감 후 확정되므로, 같은 영업일 자료가 이미 있으면 다시 받지 않는다."""
    probe = fetch_stock_trend("005930")
    if not probe:
        raise RuntimeError("종목별 투자자 매매 응답이 비어 있음")
    bizdate = probe[0].get("bizdate", "")
    try:
        prev = json.load(open(os.path.join(OUT_DIR, "latest.json"), encoding="utf-8")).get("supply") or {}
        if prev.get("bizdate") == bizdate and prev.get("picks"):
            return prev
    except Exception:  # noqa: BLE001
        pass
    rows = []
    for st in fetch_universe():
        try:
            t = fetch_stock_trend(st["code"])
            day = next((x for x in t if x.get("bizdate") == bizdate), None)
            if not day:
                continue
            close = num(day.get("closePrice")) or 0
            st["foreign_amt"] = (num(day.get("foreignerPureBuyQuant")) or 0) * close
            st["inst_amt"] = (num(day.get("organPureBuyQuant")) or 0) * close
            rows.append(st)
        except Exception:  # noqa: BLE001
            continue
        time.sleep(0.08)
    if not rows:
        raise RuntimeError("종목별 수급을 집계하지 못함")
    f_rank = [r for r in sorted(rows, key=lambda r: -r["foreign_amt"]) if r["foreign_amt"] > 0][:20]
    i_rank = [r for r in sorted(rows, key=lambda r: -r["inst_amt"]) if r["inst_amt"] > 0][:20]
    f = {r["code"]: k for k, r in enumerate(f_rank, 1)}
    i = {r["code"]: k for k, r in enumerate(i_rank, 1)}
    by = {r["code"]: r for r in rows}
    picks = []
    for code in set(f) | set(i):
        rf, ri = f.get(code), i.get(code)
        # 점수: 순위가 높을수록 가산, 양쪽 모두 있으면 추가 가산 (원본 금액은 싣지 않는다)
        score = (21 - rf if rf else 0) + (21 - ri if ri else 0) + (15 if rf and ri else 0)
        picks.append({"name": by[code]["name"], "code": code, "market": by[code]["market"],
                      "foreign_rank": rf, "inst_rank": ri,
                      "signal": "동시 매수" if rf and ri else ("외국인만" if rf else "기관만"),
                      "score": score})
    picks.sort(key=lambda p: -p["score"])
    d = bizdate
    return {"bizdate": bizdate, "date": f"{d[2:4]}.{d[4:6]}.{d[6:8]}" if len(d) == 8 else d,
            "universe": len(rows), "picks": picks[:15]}


# ---------------------------------------------------------------- 거래량 급증
def pick(d, keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d.get(k)
    return None


def fetch_surge():
    url = ("https://stock.naver.com/api/domestic/market/stock/default"
           "?tradeType=KRX&marketType=ALL&orderType=upperQuantTop&startIdx=0&pageSize=12")
    j = get(url, headers={"Referer": "https://stock.naver.com/"}).json()
    rows = j if isinstance(j, list) else (j.get("stocks") or j.get("result") or [])
    out = []
    for r in rows:
        if r.get("type") not in (None, "ST"):
            continue  # ETF·ETN 등 제외
        out.append({"name": r.get("itemname"), "code": r.get("itemcode"),
                    "market": {"0": "코스피", "1": "코스닥"}.get(str(r.get("sosok")), ""),
                    "price": num(r.get("nowPrice")),
                    "pct": num(pick(r, ["prevChangeRate", "fluctuationsRatio", "changeRate", "upDownRate"])),
                    "ratio": num(pick(r, ["quantDiffRate"]))})
    return out[:10]


# ---------------------------------------------------------------- 공시
DART_RULES = [
    ("pos", "호재성", ["공급계약체결", "자기주식취득", "자기주식소각", "주식소각", "무상증자", "현금ㆍ현물배당", "현금배당"]),
    ("neg", "악재성", ["유상증자", "전환사채", "신주인수권부사채", "교환사채", "공급계약해지", "관리종목",
                     "거래정지", "횡령", "배임", "불성실공시", "감자", "중대재해", "상장폐지", "회생절차", "부도"]),
    ("chk", "내용 확인", ["영업(잠정)실적", "잠정실적", "매출액또는손익구조", "소송", "합병", "분할", "최대주주변경",
                       "주식교환", "영업양수", "영업양도", "타법인주식", "투자판단"]),
]


def classify_dart(title):
    t = title.replace(" ", "")
    for tone, label, keys in DART_RULES:
        if any(k.replace(" ", "") in t for k in keys):
            return tone, label
    return None, None


def fetch_dart():
    out = []
    for path, mkt in (("mainY.do", "유가"), ("mainK.do", "코스닥")):
        html = get(f"https://dart.fss.or.kr/dsac001/{path}", headers={"Referer": "https://dart.fss.or.kr/"}).text
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("table tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            a_rep = tds[2].find("a")
            a_corp = tds[1].find("a")
            if not a_rep or not a_corp:
                continue
            title = re.sub(r"\s+", " ", a_rep.get_text(" ", strip=True))
            tone, label = classify_dart(title)
            if not tone:
                continue
            m = re.search(r"rcpNo=(\d+)", a_rep.get("href", ""))
            out.append({"time": tds[0].get_text(strip=True), "market": mkt,
                        "corp": a_corp.get_text(strip=True), "title": title,
                        "tone": tone, "label": label,
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={m.group(1)}" if m else ""})
        time.sleep(0.2)
    out.sort(key=lambda x: x["time"], reverse=True)
    return out[:30]


# ---------------------------------------------------------------- 해외
def fetch_world():
    out = []
    for code, name in ((".INX", "S&P 500"), (".IXIC", "나스닥"), (".DJI", "다우"), (".SOX", "필라델피아 반도체")):
        try:
            j = get(f"https://api.stock.naver.com/index/{code}/basic").json()
            out.append({"name": name, "value": num(j.get("closePrice")),
                        "pct": num(j.get("fluctuationsRatio")), "at": j.get("localTradedAt") or ""})
        except Exception:  # noqa: BLE001
            continue
    try:
        j = get("https://m.stock.naver.com/front-api/marketIndex/productDetail"
                "?category=exchange&reutersCode=FX_USDKRW").json()
        r = j.get("result") or {}
        if num(r.get("closePrice")):
            out.append({"name": "원/달러", "value": num(r.get("closePrice")),
                        "pct": num(r.get("fluctuationsRatio")), "at": r.get("localTradedAt") or ""})
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------------------------------------------------------------- 뉴스
POS_WORDS = ["급등", "반등", "상승", "강세", "최고", "순매수", "호조", "돌파", "회복", "랠리"]
NEG_WORDS = ["급락", "하락", "약세", "폭락", "순매도", "우려", "충격", "쇼크", "매도", "붕괴", "사이드카"]


def fetch_news():
    url = "https://news.google.com/rss/search?q=" + quote("코스피 증시 when:1d") + "&hl=ko&gl=KR&ceid=KR:ko"
    return parse_rss(get(url).content)


def parse_rss(content):
    root = ET.fromstring(content)
    items, pos, neg = [], 0, 0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        p = sum(1 for w in POS_WORDS if w in title)
        n = sum(1 for w in NEG_WORDS if w in title)
        pos += 1 if p > n else 0
        neg += 1 if n > p else 0
        items.append({"title": title, "url": it.findtext("link") or "",
                      "source": src.text if src is not None else "",
                      "at": it.findtext("pubDate") or "",
                      "tone": "pos" if p > n else ("neg" if n > p else "neu")})
    total = len(items)
    return {"total": total, "pos": pos, "neg": neg, "items": items[:8]}


# ---------------------------------------------------------------- 성적표
def update_history(supply):
    """동시 매수 신호의 다음 거래일 등락을 기록한다. 빗나간 것도 지우지 않는다."""
    path = os.path.join(OUT_DIR, "history.json")
    try:
        hist = json.load(open(path, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        hist = []
    if supply and supply.get("date"):
        if not any(h["date"] == supply["date"] for h in hist):
            both = [p for p in supply["picks"] if p["signal"] == "동시 매수"][:5]
            if both:
                hist.append({"date": supply["date"],
                             "picks": [{"name": p["name"], "code": p["code"]} for p in both]})
    for h in hist[-6:]:
        if h.get("done"):
            continue
        ymd = "20" + h["date"].replace(".", "")
        filled = 0
        for p in h["picks"]:
            if p.get("next_pct") is not None:
                filled += 1
                continue
            try:
                rows = fetch_daily(p["code"], days=30)
                idx = next((k for k, r in enumerate(rows) if r["d"] == ymd), None)
                # 장 마감 전의 당일 봉은 확정값이 아니므로 오늘 날짜 봉은 결과로 쓰지 않는다
                if idx is not None and idx + 1 < len(rows) and rows[idx + 1]["d"] < now_kst().strftime("%Y%m%d"):
                    p["next_date"] = rows[idx + 1]["d"]
                    p["next_pct"] = round((rows[idx + 1]["c"] / rows[idx]["c"] - 1) * 100, 2)
                    filled += 1
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
        if filled == len(h["picks"]):
            h["done"] = True
    hist = hist[-30:]
    json.dump(hist, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return hist[-8:]


# ---------------------------------------------------------------- 판정
def verdict(index, flow, world, news):
    score, reasons = 0.0, []
    k = (index or {}).get("kospi") or {}
    if k.get("pct") is not None:
        if k["pct"] >= 0.3:
            score += 1; reasons.append(("pos", f"코스피 {k['pct']:+.2f}%"))
        elif k["pct"] <= -0.3:
            score -= 1; reasons.append(("neg", f"코스피 {k['pct']:+.2f}%"))
        else:
            reasons.append(("neu", f"코스피 보합 {k['pct']:+.2f}%"))
    f = (flow or {}).get("kospi") or {}
    if f.get("foreign") is not None:
        if f["foreign"] > 0:
            score += 1; reasons.append(("pos", f"외국인 순매수 {f['foreign']:+,.0f}억"))
        elif f["foreign"] < 0:
            score -= 1; reasons.append(("neg", f"외국인 순매도 {f['foreign']:+,.0f}억"))
    if f.get("inst") is not None and f["inst"] != 0:
        score += 0.5 if f["inst"] > 0 else -0.5
        reasons.append(("pos" if f["inst"] > 0 else "neg", f"기관 {f['inst']:+,.0f}억"))
    nq = next((w for w in (world or []) if w["name"] == "나스닥" and w.get("pct") is not None), None)
    if nq:
        if nq["pct"] >= 0.5:
            score += 1; reasons.append(("pos", f"나스닥 {nq['pct']:+.2f}%"))
        elif nq["pct"] <= -0.5:
            score -= 1; reasons.append(("neg", f"나스닥 {nq['pct']:+.2f}%"))
    if news and news.get("total"):
        diff = news["pos"] - news["neg"]
        if abs(diff) >= 3:
            score += 0.5 if diff > 0 else -0.5
            reasons.append(("pos" if diff > 0 else "neg", f"뉴스 제목 긍정 {news['pos']} · 부정 {news['neg']}"))
    if not reasons:
        return {"label": "판정 보류", "tone": "neu", "score": None, "reasons": [("neu", "판정에 쓸 자료를 받지 못했습니다")]}
    label, tone = ("우호적", "pos") if score >= 2 else (("주의", "neg") if score <= -2 else ("관망", "neu"))
    return {"label": label, "tone": tone, "score": score, "reasons": reasons}


def market_open(t):
    return t.weekday() < 5 and dt.time(9, 0) <= t.time() <= dt.time(15, 40)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    t = now_kst()
    index = {
        "kospi": run("지수 코스피", lambda: fetch_index("KOSPI")),
        "kosdaq": run("지수 코스닥", lambda: fetch_index("KOSDAQ")),
    }
    flow = {
        "kospi": run("투자자 수급 코스피", lambda: fetch_index_flow("KOSPI")),
        "kosdaq": run("투자자 수급 코스닥", lambda: fetch_index_flow("KOSDAQ")),
    }
    supply = run("종목별 수급", build_supply)
    surge = run("거래량 급증", fetch_surge, default=[])
    watch = run("관심종목 시세", build_watch, default=[])
    dart = run("공시 DART", fetch_dart, default=[])
    world = run("해외 지수·환율", fetch_world, default=[])
    news = run("뉴스 제목", fetch_news)
    SOURCES["공매도·신용잔고"] = {"ok": False, "msg": "1차 버전에서는 수집하지 않음", "ms": 0, "planned": True}
    history = run("신호 성적표", lambda: update_history(supply), default=[])
    if not history and "빈 응답" in SOURCES["신호 성적표"]["msg"]:
        SOURCES["신호 성적표"] = {"ok": True, "msg": "아직 쌓인 기록 없음", "ms": 0}

    doc = {
        "generated_at": t.isoformat(timespec="seconds"),
        "market_open": market_open(t),
        "sources": SOURCES,
        "verdict": verdict(index, flow, world, news),
        "index": index, "flow": flow, "supply": supply, "surge": surge,
        "watch": watch, "stages": DISPARITY_STAGES, "dart": dart,
        "world": world, "news": news, "history": history,
    }
    ok = sum(1 for s in SOURCES.values() if s.get("ok"))
    doc["summary"] = {"ok": ok, "total": len([s for s in SOURCES.values() if not s.get("planned")])}
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as fp:
        json.dump(doc, fp, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"generated_at": doc["generated_at"], "sources": SOURCES}, ensure_ascii=False, indent=1))
    # 전부 실패해도 0으로 끝낸다: 실패 내역이 latest.json 에 남아 페이지에서 보이는 편이 낫다.
    return 0


if __name__ == "__main__":
    sys.exit(main())
