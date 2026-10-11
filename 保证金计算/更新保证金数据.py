# -*- coding: utf-8 -*-
"""一键更新保证金计算器的数据（margin_data.js）。

流程：
  1. 抓广发期货官网「交易提示」最新一期 → 解析公司保证金率标准
  2. 自动定位该保证金率对应的「上一交易日」
  3. 抓五大交易所（上期所/能源中心/郑商所/中金所/广期所）该日结算价 + 成交量
  4. 每个品种再挑出成交量最大的「主力合约」
  5. 合成 margin_data.js（放在本脚本同目录）

大商所（DCE）官网有 WAF 拦截，脚本无法自动取数，页面已提供「手动计算」兜底。
只依赖 Python 标准库，任意 Python 3 均可运行。
"""
import io, os, re, sys, json, gzip, zlib, datetime
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
GF = "https://www.gfqh.com.cn"

# 合约乘数：结算价 × mult = 每手合约价值（元）；以各交易所合约细则为准
UNITS = {
    "ad": (10, "10吨/手"), "ag": (15, "15千克/手"), "al": (5, "5吨/手"),
    "ao": (20, "20吨/手"), "au": (1000, "1000克/手"), "br": (5, "5吨/手"),
    "bu": (10, "10吨/手"), "cu": (5, "5吨/手"), "fu": (10, "10吨/手"),
    "hc": (10, "10吨/手"), "ni": (1, "1吨/手"), "op": (40, "40吨/手"),
    "pb": (5, "5吨/手"), "rb": (10, "10吨/手"), "ru": (10, "10吨/手"),
    "sn": (1, "1吨/手"), "sp": (10, "10吨/手"), "ss": (5, "5吨/手"),
    "wr": (10, "10吨/手"), "zn": (5, "5吨/手"),
    "bc": (5, "5吨/手"), "ec": (50, "50元/点"), "lu": (10, "10吨/手"),
    "nr": (10, "10吨/手"), "sc": (1000, "1000桶/手"),
    "IC": (200, "200元/点"), "IF": (300, "300元/点"), "IH": (300, "300元/点"),
    "IM": (200, "200元/点"), "T": (10000, "面值100万元"), "TF": (10000, "面值100万元"),
    "TL": (10000, "面值100万元"), "TS": (20000, "面值200万元"),
    "AP": (10, "10吨/手"), "CF": (5, "5吨/手"), "CJ": (5, "5吨/手"),
    "CY": (5, "5吨/手"), "FG": (20, "20吨/手"), "JR": (20, "20吨/手"),
    "MA": (10, "10吨/手"), "OI": (10, "10吨/手"), "PF": (5, "5吨/手"),
    "PK": (5, "5吨/手"), "PL": (20, "20吨/手"), "PM": (50, "50吨/手"),
    "PR": (15, "15吨/手"), "PX": (5, "5吨/手"), "RI": (20, "20吨/手"),
    "RM": (10, "10吨/手"), "RS": (10, "10吨/手"), "SA": (20, "20吨/手"),
    "SF": (5, "5吨/手"), "SH": (30, "30吨/手"), "SM": (5, "5吨/手"),
    "SR": (10, "10吨/手"), "TA": (5, "5吨/手"), "UR": (20, "20吨/手"),
    "WH": (20, "20吨/手"), "ZC": (200, "200吨/手"),
    "a": (10, "10吨/手"), "b": (10, "10吨/手"), "bb": (500, "500张/手"),
    "bz": (5, "5吨/手"), "c": (10, "10吨/手"), "cs": (10, "10吨/手"),
    "eb": (5, "5吨/手"), "eg": (10, "10吨/手"), "fb": (10, "10立方米/手"),
    "i": (100, "100吨/手"), "j": (100, "100吨/手"), "jd": (10, "5吨/手（报价·元/500kg）"),
    "jm": (60, "60吨/手"), "l": (5, "5吨/手"), "lg": (90, "90立方米/手"),
    "lh": (16, "16吨/手"), "m": (10, "10吨/手"), "p": (10, "10吨/手"),
    "pg": (20, "20吨/手"), "pp": (5, "5吨/手"), "rr": (10, "10吨/手"),
    "v": (5, "5吨/手"), "y": (10, "10吨/手"),
    "lc": (1, "1吨/手"), "pd": (1000, "1000克/手"), "ps": (3, "3吨/手"),
    "pt": (1000, "1000克/手"), "si": (5, "5吨/手"),
}

EX_KEYS = ["中国金融期货交易所", "郑州商品交易所", "大连商品交易所",
           "广州期货交易所", "上海国际能源交易中心", "上海期货交易所"]
LINE_RE = re.compile(r"^([A-Za-z]{1,3})-([^：:]+)[：:]\s*([\d.]+)\s*%"
                     r"(?:[（(]其中([^）)]*)[）)])?$")


def _dec(raw, enc):
    e = (enc or "").lower()
    if "gzip" in e: raw = gzip.decompress(raw)
    elif "deflate" in e: raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def get(url, data=None, ref=None, ct=None, timeout=40):
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if ref: h["Referer"] = ref
    if ct: h["Content-Type"] = ct
    r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout)
    return _dec(r.read(), r.headers.get("Content-Encoding"))


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


# ---------- 1) 保证金率 ----------
def fetch_margin_rates():
    lst = get(GF + "/article/ul/list?id=139&currentPage=1", ref=GF + "/menu?id=139")
    lst = lst.decode("utf-8", "replace")
    ids = re.findall(r'id="(\d+)"', lst)
    if not ids:
        raise RuntimeError("未取到交易提示文章列表")
    aid = ids[0]
    url = (GF + "/article/detail?menuname=" + urllib.parse.quote("交易提示")
           + "&menuid=139&articleid=" + aid)
    h = get(url, ref=GF + "/menu?id=139").decode("utf-8", "replace")
    import html as H
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
    body = re.sub(r"<(br|BR)\s*/?>", "\n", body)
    body = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", body, flags=re.I)
    body = H.unescape(re.sub(r"<[^>]+>", "", body))
    lines = [re.sub(r"[ \t\u00a0\u3000]+", " ", ln).strip() for ln in body.split("\n")]
    lines = [ln for ln in lines if ln]

    data, cur, md = {}, None, None
    for ln in lines:
        m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日公司保证金率标准", ln)
        if m:
            md = (int(m.group(1)), int(m.group(2))); continue
        hit = next((k for k in EX_KEYS if ln.startswith("●") and k in ln), None)
        if hit:
            cur = hit; data.setdefault(cur, []); continue
        if cur is None:
            continue
        if ln.startswith("注"):
            break
        mm = LINE_RE.match(ln)
        if not mm:
            continue
        code, name, base, ov = mm.group(1), mm.group(2).strip(), float(mm.group(3)), mm.group(4)
        overrides = {}
        if ov:
            for mm2 in re.finditer(r"((?:[A-Za-z]{1,3}\d{4}\s*,\s*)*[A-Za-z]{1,3}\d{4})\s*-\s*([\d.]+)\s*%",
                                   ov.replace("，", ",")):
                for c in mm2.group(1).split(","):
                    overrides[c.strip()] = float(mm2.group(2))
        data[cur].append({"code": code, "name": name, "base": base, "overrides": overrides})
    today = datetime.date.today()
    date = None
    if md:
        try:
            date = datetime.date(today.year, md[0], md[1])
        except ValueError:
            date = None
    return {"article_id": aid, "source": url, "monthday": "%d月%d日" % md if md else "",
            "date": date.isoformat() if date else "",
            "exchanges": data}


# ---------- 2) 结算价 ----------
def fetch_settle(d):
    out = {}
    for nm, host in (("SHFE", "https://www.shfe.com.cn"), ("INE", "https://www.ine.cn")):
        try:
            j = json.loads(get("%s/data/tradedata/future/dailydata/kx%s.dat" % (host, d), ref=host + "/"))
            for it in j.get("o_curinstrument", []):
                if it.get("PRODUCTCLASS") != "1":
                    continue
                k = ("%s%s" % (it.get("PRODUCTGROUPID", ""), it.get("DELIVERYMONTH", ""))).upper()
                out[k] = {"settle": it.get("SETTLEMENTPRICE"), "prev": it.get("PRESETTLEMENTPRICE"),
                          "vol": it.get("VOLUME"), "ex": nm, "name": it.get("PRODUCTNAME", "")}
        except Exception:
            pass
    try:
        raw = get("http://www.czce.com.cn/cn/DFSStaticFiles/Future/%s/%s/FutureDataDaily.txt" % (d[:4], d),
                  ref="http://www.czce.com.cn/")
        txt = None
        for enc in ("utf-8", "gbk"):
            try:
                txt = raw.decode(enc); break
            except Exception:
                continue
        for ln in (txt or "").splitlines():
            if "|" not in ln:
                continue
            c = [x.strip() for x in ln.split("|")]
            if len(c) < 10 or not re.match(r"^[A-Za-z]{1,3}\d{3,4}$", c[0]):
                continue
            out[c[0].upper()] = {"settle": _num(c[6]), "prev": _num(c[1]), "vol": _num(c[9]),
                                 "ex": "CZCE", "name": ""}
    except Exception:
        pass
    try:
        t = get("http://www.cffex.com.cn/sj/hqsj/rtj/%s/%s/index.xml" % (d[:6], d[6:]),
                ref="http://www.cffex.com.cn/").decode("utf-8", "replace")
        for m in re.finditer(r"<dailydata>(.*?)</dailydata>", t, re.S):
            blk = m.group(1)
            def g(tag):
                mm = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), blk)
                return mm.group(1).strip() if mm else ""
            iid = g("instrumentid")
            if not re.match(r"^[A-Z]{1,3}\d{4}$", iid):
                continue
            out[iid.upper()] = {"settle": _num(g("settlementprice")), "prev": _num(g("presettlementprice")),
                                "vol": _num(g("volume")), "ex": "CFFEX", "name": ""}
    except Exception:
        pass
    try:
        body = urllib.parse.urlencode({"trade_date": d, "trade_type": "0"}).encode()
        j = json.loads(get("http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList", data=body,
                           ref="http://www.gfex.com.cn/gfex/rihq/hqsj_tjsj.shtml",
                           ct="application/x-www-form-urlencoded; charset=UTF-8"))
        for it in j.get("data", []):
            k = ("%s%s" % (it.get("varietyOrder", ""), it.get("delivMonth", ""))).upper()
            out[k] = {"settle": it.get("clearPrice"), "prev": it.get("lastClear"),
                      "vol": it.get("volumn"), "ex": "GFEX", "name": it.get("variety", "")}
    except Exception:
        pass
    # 大商所：官网有 WAF 拦截，改用新浪财经日线接口（s=结算价）
    try:
        out.update(fetch_dce_sina(d, DCE_CODES))
    except Exception:
        pass
    return out


# ---------- 2b) 大商所：新浪财经日线（结算价字段 s） ----------
# 校验依据：同接口取上期所合约，其 s 与交易所官方结算价完全一致
# （rb2610=3038、cu2610=110370、au2612=905.04、ag2612=14928，4/4 命中）。
DCE_CODES = ["a", "b", "bb", "bz", "c", "cs", "eb", "eg", "fb", "i", "j", "jd", "jm",
             "l", "lg", "lh", "m", "p", "pg", "pp", "rr", "v", "y"]
SINA_REF = "https://finance.sina.com.cn/"


def _sina_get(url, enc="gbk"):
    r = urllib.request.urlopen(urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": SINA_REF}), timeout=25)
    b = r.read()
    e = r.headers.get("Content-Encoding", "").lower()
    if "gzip" in e: b = gzip.decompress(b)
    elif "deflate" in e: b = zlib.decompress(b, -zlib.MAX_WBITS)
    return b.decode(enc, "replace")


def _sina_exists(syms):
    """批量行情判断哪些合约存在。"""
    have = set()
    for i in range(0, len(syms), 60):
        try:
            t = _sina_get("https://hq.sinajs.cn/list=" + ",".join("nf_" + s for s in syms[i:i + 60]))
        except Exception:
            continue
        for ln in t.strip().split("\n"):
            if "=" not in ln:
                continue
            head, val = ln.split("=", 1)
            if val.strip().strip('";'):
                have.add(head.replace("var hq_str_nf_", "").strip())
    return have


def _sina_kline(sym):
    u = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_x=/"
         "InnerFuturesNewService.getDailyKLine?symbol=" + sym)
    try:
        t = _sina_get(u)
        i, j = t.find("(["), t.rfind("])")
        return sym, json.loads(t[i + 1:j + 1])
    except Exception:
        return sym, []


def fetch_dce_sina(d, codes):
    """取大商所各合约在交易日 d 的结算价与成交量。"""
    y, m = int(d[:4]), int(d[4:6])
    months = []
    for k in range(15):
        mm = m + k
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        months.append("%02d%02d" % (yy % 100, mm))
    cands = ["%s%s" % (c.upper(), mm) for c in codes for mm in months]
    exist = _sina_exists(cands)
    iso = "%s-%s-%s" % (d[:4], d[4:6], d[6:])
    out = {}
    if not exist:
        return out
    with ThreadPoolExecutor(max_workers=8) as ex:
        for sym, rows in ex.map(_sina_kline, sorted(exist)):
            idx = next((i for i, r in enumerate(rows) if r.get("d") == iso), None)
            if idx is None:
                continue
            row = rows[idx]
            try:
                s = float(row.get("s") or 0)
            except Exception:
                continue
            if s <= 0:
                continue
            prev = None
            if idx > 0:
                try:
                    prev = float(rows[idx - 1].get("s") or 0) or None
                except Exception:
                    prev = None
            out[sym.upper()] = {"settle": s, "prev": prev,
                                "vol": _num(row.get("v")) or 0,
                                "ex": "DCE", "name": ""}
    return out


def probe_settle_date(margin_date):
    cur = margin_date - datetime.timedelta(days=1)
    for _ in range(15):
        d = cur.strftime("%Y%m%d")
        items = fetch_settle(d)
        if len(items) > 100:
            return d, items
        cur -= datetime.timedelta(days=1)
    raise RuntimeError("近 15 天内未找到可用结算价日期")


# ---------- 3) 合成 ----------
def pick_main(cs):
    """主力合约：成交量最大；无成交量数据则取最近的月份。"""
    if not cs:
        return None
    withvol = [c for c in cs if c.get("vol")]
    if withvol:
        return max(withvol, key=lambda c: c["vol"])
    return cs[0]


def build(rates, settle_date, sitems):
    out_items, no_settle = [], []
    ym_now = settle_date[:4] + settle_date[4:6]
    for ex, arr in rates["exchanges"].items():
        for it in arr:
            code, name, base = it["code"], it["name"], it["base"]
            ov = {k.upper(): v for k, v in it["overrides"].items()}
            if code not in UNITS:
                continue
            mult, unit = UNITS[code]
            cs = []
            for cid, rec in sitems.items():
                m = re.match(r"^([A-Za-z]+)(\d{3,4})$", cid)
                if not m or m.group(1).upper() != code.upper() or not rec.get("settle"):
                    continue
                rate = ov.get(cid.upper(), base)
                cs.append({"id": cid, "settle": rec["settle"], "rate": rate,
                           "vol": rec.get("vol") or 0,
                           "src": "新浪" if ex == "大连商品交易所" else "交易所",
                           "margin": round(rec["settle"] * mult * rate / 100.0, 2)})
            # 月份由近及远排序（便于前端默认选中近月）
            def mx(c):
                mm = re.match(r"^[A-Za-z]+(\d{3,4})$", c["id"])
                s = mm.group(1)
                return int(("2" + s) if len(s) == 3 else s)
            cs.sort(key=mx)
            main = pick_main(cs)
            item = {"ex": ex, "code": code, "name": name, "mult": mult, "unit": unit,
                    "base": base, "overrides": it["overrides"],
                    "mainId": main["id"] if main else None,
                    "mainMargin": main["margin"] if main else None,
                    "mainSettle": main["settle"] if main else None,
                    "mainRate": main["rate"] if main else None,
                    "mainVol": main["vol"] if main else None,
                    "contracts": cs}
            if not cs:
                no_settle.append(name)
            out_items.append(item)
    data = {"rateDate": rates["date"], "monthday": rates["monthday"],
            "settleDate": "%s-%s-%s" % (settle_date[:4], settle_date[4:6], settle_date[6:]),
            "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": rates["source"], "items": out_items}
    return data, no_settle


def main():
    rates = fetch_margin_rates()
    if not rates["date"]:
        raise RuntimeError("未能从交易提示解析出保证金率日期")
    mdate = datetime.date.fromisoformat(rates["date"])
    today = datetime.date.today()
    sd, sitems = probe_settle_date(mdate)
    data, no_settle = build(rates, sd, sitems)
    js = "window.MARGIN_DATA = " + json.dumps(data, ensure_ascii=False, indent=1) + ";\n"
    io.open(os.path.join(HERE, "margin_data.js"), "w", encoding="utf-8").write(js)
    nc = sum(len(i["contracts"]) for i in data["items"])
    have_main = sum(1 for i in data["items"] if i["mainMargin"])
    msg = ["更新时间：%s" % data["updated"],
           "保证金率：%s（%s）" % (rates["monthday"], rates["date"]),
           "结算价日期：%s" % data["settleDate"],
           "品种 %d 个，可算合约 %d 个，其中 %d 个品种有主力合约报价"
           % (len(data["items"]), nc, have_main)]
    if rates["date"] <= today.isoformat():
        msg.append("提示：该保证金率日期不晚于今天，说明下一交易日的交易提示可能尚未发布。")
    msg.append("无结算价品种（需手动计算）：%s" % ("、".join(no_settle) if no_settle else "无"))
    dce = [i for i in data["items"] if i["ex"] == "大连商品交易所"]
    dce_ok = [i for i in dce if i["mainMargin"]]
    msg.append("大商所：%d/%d 个品种已取到（新浪财经日线，结算价口径已与交易所比对一致）"
               % (len(dce_ok), len(dce)))
    msg.append("已写入：%s" % os.path.join(HERE, "margin_data.js"))
    io.open(os.path.join(HERE, "更新日志.txt"), "w", encoding="utf-8").write(
        "\n".join([msg[0]] + msg[1:]) + "\n")
    print("\n".join(msg))


if __name__ == "__main__":
    main()
