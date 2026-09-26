"""
QX Signal Pro — Backend Server v3
===================================
Run: python server.py
URL: http://localhost:8000/quotex_signal.html

Install: pip install yfinance feedparser numpy
"""
import http.server, json, urllib.parse, os, sys, math, traceback, time
from datetime import datetime, timezone

try:
    import yfinance as yf
    HAS_YF = True
except:
    HAS_YF = False

try:
    import numpy as np
    HAS_NP = True
except:
    HAS_NP = False

try:
    import feedparser, re as _re
    HAS_FP = True
except:
    HAS_FP = False

PORT = 8000

NEWS_FEEDS = {
    "Reuters":       "https://feeds.reuters.com/reuters/businessNews",
    "BBC Business":  "https://feeds.bbci.co.uk/news/business/rss.xml",
    "Economic Times":"https://economictimes.indiatimes.com/markets/rss.cms",
    "FX Street":     "https://www.fxstreet.com/rss/news",
    "Investing.com": "https://www.investing.com/rss/news.rss",
}

# ── Cache ────────────────────────────────────────────────────
_cache = {}
CACHE_TTL = 60  # seconds

def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry['ts'] < CACHE_TTL:
        return entry['data']
    return None

def cache_set(key, data):
    _cache[key] = {'data': data, 'ts': time.time()}


def safe_float(v):
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except:
        return None

def get_col(df, *names):
    for n in names:
        if n in df.columns:
            return df[n].tolist()
        for col in df.columns:
            if str(col).lower() == n.lower():
                return df[col].tolist()
    return None


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *a): print(f"  {fmt%a}")
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)
        path = p.path

        if path == "/api/chart":
            sym  = q.get("symbol",["EURUSD=X"])[0].strip().upper()
            per  = q.get("period", ["3mo"])[0].strip()
            ivl  = q.get("interval",["1h"])[0].strip()
            self.chart(sym, per, ivl)
        elif path == "/api/news":
            sym = q.get("symbol",[""])[0].strip()
            self.news(sym)
        elif path == "/api/test":
            self.send_json({"ok":True,"yfinance":HAS_YF,"numpy":HAS_NP,"feedparser":HAS_FP})
        elif path in ("/","/index.html"):
            self.html("legend_oracle.html")
        elif path == "/quotex_signal.html":
            self.html("quotex_signal.html")
        else:
            try: super().do_GET()
            except: self.send_response(404); self.end_headers()

    # ── CHART ────────────────────────────────────────────────
    def chart(self, sym, period, interval):
        cache_key = f"{sym}_{period}_{interval}"
        cached = cache_get(cache_key)
        if cached:
            print(f"  [CACHE] {cache_key}")
            return self.send_json(cached)

        if not HAS_YF:
            return self.send_json({"error":"pip install yfinance"},500)

        valid_ivl = ["1m","2m","5m","15m","30m","60m","90m","1h","1d","5d","1wk","1mo","3mo"]
        if interval not in valid_ivl: interval = "1h"

        print(f"\n  [CHART] {sym} period={period} interval={interval}")
        try:
            tk   = yf.Ticker(sym)
            hist = None
            for per in [period,"max","1y","6mo","3mo"]:
                try:
                    h = tk.history(period=per, interval=interval)
                    if h is not None and not h.empty and len(h)>=15:
                        hist=h; print(f"  Got {len(h)} rows (period={per})"); break
                    print(f"  {per}: {len(h) if h is not None else 0} rows")
                except Exception as e:
                    print(f"  {per} failed: {e}")

            if hist is None or hist.empty:
                return self.send_json({"error":f"No data for {sym}"},404)

            print(f"  Columns: {list(hist.columns)}")

            closes  = get_col(hist,"Close")
            highs   = get_col(hist,"High")
            lows    = get_col(hist,"Low")
            opens   = get_col(hist,"Open")
            volumes = get_col(hist,"Volume")
            times   = list(hist.index)

            if not closes:
                return self.send_json({"error":"No Close column"},404)

            name,currency = sym,""
            try:
                fi = tk.fast_info
                name = getattr(fi,'long_name',sym) or sym
                currency = getattr(fi,'currency','') or ''
            except: pass

            candles = []
            for i,dt in enumerate(times):
                try:
                    c2 = safe_float(closes[i])
                    h2 = safe_float(highs[i])  if highs   else c2
                    l2 = safe_float(lows[i])   if lows    else c2
                    o2 = safe_float(opens[i])  if opens   else c2
                    v2 = int(float(volumes[i])) if volumes else 0
                    if not o2: o2=c2
                    if not h2: h2=c2
                    if not l2: l2=c2
                    if c2 and c2>0:
                        candles.append({"t":int(dt.timestamp()),
                            "o":round(o2,6),"h":round(h2,6),
                            "l":round(l2,6),"c":round(c2,6),"v":v2})
                except: continue

            print(f"  Valid: {len(candles)} candles")
            if len(candles)<15:
                return self.send_json({"error":f"Only {len(candles)} candles"},404)

            # ── Server-side technical analysis ──────────────
            analysis = self.analyze(candles, sym)

            result = {"symbol":sym,"name":name,"currency":currency,
                      "candles":candles,"count":len(candles),"analysis":analysis}
            cache_set(cache_key, result)
            self.send_json(result)

        except Exception as e:
            print(f"  ERROR:\n{traceback.format_exc()}")
            self.send_json({"error":str(e)},500)

    # ── ANALYSIS ENGINE (Python — more accurate than JS) ────
    def analyze(self, candles, sym):
        try:
            closes = [d['c'] for d in candles]
            highs  = [d['h'] for d in candles]
            lows   = [d['l'] for d in candles]
            n      = len(closes)

            def ema(arr, p):
                k = 2/(p+1); acc = []
                for i,v in enumerate(arr):
                    acc.append(v if i==0 else v*k+acc[-1]*(1-k))
                return acc

            def rsi(arr, p=14):
                g=l=0
                for i in range(1,min(p+1,len(arr))):
                    d=arr[i]-arr[i-1]
                    if d>0: g+=d
                    else: l-=d
                g/=p; l/=p; out=[50]
                for i in range(p+1,len(arr)):
                    d=arr[i]-arr[i-1]
                    g=(g*(p-1)+(d if d>0 else 0))/p
                    l=(l*(p-1)+(-d if d<0 else 0))/p
                    out.append(100 if l==0 else 100-100/(1+g/l))
                return out

            def sma(arr, p):
                return [None if i<p-1 else sum(arr[i-p+1:i+1])/p for i in range(len(arr))]

            def atr_calc(c, p=14):
                tr=[c[0]['h']-c[0]['l']]
                for i in range(1,len(c)):
                    tr.append(max(c[i]['h']-c[i]['l'],
                                  abs(c[i]['h']-c[i-1]['c']),
                                  abs(c[i]['l']-c[i-1]['c'])))
                atr_s=sma(tr,p)
                return [x for x in atr_s if x is not None][-1] if atr_s else closes[-1]*0.001

            # Indicators
            e9  = ema(closes,9)
            e21 = ema(closes,21)
            e50 = ema(closes,50)
            rsi14 = rsi(closes,14)
            macd_line = [e9[i]-e21[i] for i in range(n)]
            sig_line  = ema(macd_line,9)
            macd_hist = [macd_line[i]-sig_line[i] for i in range(n)]
            cur_atr   = atr_calc(candles)

            # Bollinger
            sma20 = sma(closes,20)
            boll_u=boll_l=None
            for i in range(n-1,-1,-1):
                if sma20[i] is not None:
                    sl=closes[max(0,i-19):i+1]
                    mn=sma20[i]
                    sd=math.sqrt(sum((x-mn)**2 for x in sl)/len(sl))
                    boll_u=mn+2*sd; boll_l=mn-2*sd; break

            cur     = closes[-1]
            cur_rsi = rsi14[-1] if rsi14 else 50
            cur_mh  = macd_hist[-1]
            cur_e9  = e9[-1]; cur_e21=e21[-1]; cur_e50=e50[-1]

            boll_pct= ((cur-boll_l)/(boll_u-boll_l)*100) if boll_u and boll_l and boll_u!=boll_l else 50

            uptrend = cur_e9>cur_e21 and cur_e21>cur_e50
            dntrend = cur_e9<cur_e21 and cur_e21<cur_e50
            trend   = "UP" if uptrend else "DOWN" if dntrend else "SIDEWAYS"

            # Stochastic
            stoch_v=50
            if n>=14:
                sl14=candles[-14:]
                hh=max(d['h'] for d in sl14); ll=min(d['l'] for d in sl14)
                stoch_v=((cur-ll)/(hh-ll)*100) if hh!=ll else 50

            # Scoring
            score=50
            # RSI
            if cur_rsi<25: score+=22
            elif cur_rsi<35: score+=14
            elif cur_rsi<45: score+=6
            elif cur_rsi>75: score-=22
            elif cur_rsi>65: score-=14
            elif cur_rsi>55: score-=6
            # MACD
            prev_mh = macd_hist[-2] if len(macd_hist)>1 else 0
            if cur_mh>0 and prev_mh<=0: score+=20
            elif cur_mh<0 and prev_mh>=0: score-=20
            elif cur_mh>0: score+=9
            elif cur_mh<0: score-=9
            # EMA
            if uptrend: score+=22
            elif dntrend: score-=22
            elif cur>cur_e9: score+=7
            elif cur<cur_e9: score-=7
            # Stoch
            if stoch_v<20: score+=13
            elif stoch_v<30: score+=7
            elif stoch_v>80: score-=13
            elif stoch_v>70: score-=7
            # Bollinger
            if boll_pct<15: score+=11
            elif boll_pct<25: score+=5
            elif boll_pct>85: score-=11
            elif boll_pct>75: score-=5
            # Price vs EMA9
            if cur>cur_e9*1.001: score+=8
            elif cur<cur_e9*0.999: score-=8

            score = max(0,min(100,round(score)))
            is_up = score>=50
            conf  = score if is_up else 100-score

            # Support/Resistance (pivot)
            rec = candles[-20:]
            rh  = max(d['h'] for d in rec)
            rl  = min(d['l'] for d in rec)
            pp  = (rh+rl+cur)/3
            r1=2*pp-rl; r2=pp+(rh-rl); s1=2*pp-rh; s2=pp-(rh-rl)

            # Weekly returns stats
            rets = [(closes[i]-closes[i-1])/closes[i-1]*100 for i in range(1,n)]
            win_rate = (len([r for r in rets if r>0])/len(rets)*100) if rets else 50
            avg_ret  = sum(rets)/len(rets) if rets else 0
            max_gain = max(rets) if rets else 0
            max_loss = min(rets) if rets else 0
            ann_vol  = (math.sqrt(sum((r-avg_ret)**2 for r in rets)/len(rets))*math.sqrt(52)) if rets else 10

            # Candlestick patterns (last 3 candles)
            patterns = self.detect_patterns(candles)

            # Adjust score with patterns
            for p in patterns:
                w = round((p['strength']-60)/3)
                if p['dir']=='bull': score+=w
                elif p['dir']=='bear': score-=w
            score = max(0,min(100,round(score)))
            is_up = score>=50
            conf  = score if is_up else 100-score

            # Signal string
            if conf>=75:
                signal = ("UP ↑ LO" if is_up else "DOWN ↓ LO")
                sig_strength = "STRONG"
            elif conf>=62:
                signal = ("UP ↑ POSSIBLE" if is_up else "DOWN ↓ POSSIBLE")
                sig_strength = "MODERATE"
            else:
                signal = "WAIT — CLEAR NAHI"
                sig_strength = "WEAK"

            # Future price predictions
            atr_pct = cur_atr/cur*100 if cur>0 else 1
            dmult   = 1 if is_up else -1
            cf      = conf/100

            def pred_price(mult):
                return round(cur + dmult*cur_atr*mult*cf, 6)

            preds = {
                "5m":  {"price":pred_price(0.3), "rate": min(85,round(conf*0.95))},
                "10m": {"price":pred_price(0.6), "rate": min(80,round(conf*0.90))},
                "15m": {"price":pred_price(1.0), "rate": min(78,round(conf*0.87))},
                "1h":  {"price":pred_price(2.5), "rate": min(80,round(conf*0.92))},
                "1d":  {"price":pred_price(5.0), "rate": min(82,round(conf*0.94))},
            }

            return {
                "score": score, "conf": conf, "is_up": is_up,
                "signal": signal, "sig_strength": sig_strength,
                "trend": trend,
                "rsi": round(cur_rsi,1),
                "macd_hist": round(cur_mh,6),
                "stoch": round(stoch_v,1),
                "ema9": round(cur_e9,6), "ema21": round(cur_e21,6),
                "boll_pct": round(boll_pct,1),
                "boll_u": round(boll_u,6) if boll_u else None,
                "boll_l": round(boll_l,6) if boll_l else None,
                "atr": round(cur_atr,6), "atr_pct": round(atr_pct,2),
                "pivot": round(pp,6), "r1": round(r1,6), "r2": round(r2,6),
                "s1": round(s1,6), "s2": round(s2,6),
                "win_rate": round(win_rate,1), "ann_vol": round(ann_vol,1),
                "max_gain": round(max_gain,2), "max_loss": round(max_loss,2),
                "patterns": patterns, "predictions": preds,
                "uptrend": uptrend, "dntrend": dntrend,
            }
        except Exception as e:
            print(f"  Analysis error: {traceback.format_exc()}")
            return {"error": str(e), "score":50,"conf":50,"is_up":True,"signal":"WAIT","sig_strength":"WEAK","trend":"SIDEWAYS"}

    def detect_patterns(self, candles):
        pats = []
        n = len(candles)
        if n < 3: return pats
        d=candles[-1]; d1=candles[-2]; d2=candles[-3]

        body  = lambda v: abs(v['c']-v['o'])
        total = lambda v: v['h']-v['l']
        isup  = lambda v: v['c']>=v['o']
        uwk   = lambda v: v['h']-max(v['c'],v['o'])
        lwk   = lambda v: min(v['c'],v['o'])-v['l']

        bd=body(d); td=total(d)
        if td>0:
            # Doji
            if bd/td<0.1:
                pats.append({'name':'Doji','dir':'neut','strength':65,'desc':'Open≈Close — market indecisive, reversal possible'})
            # Hammer
            if not isup(d1) and lwk(d)>bd*2 and uwk(d)<bd*0.5:
                pats.append({'name':'Hammer','dir':'bull','strength':78,'desc':'Lower wick long — buyers pushed back sellers. UP signal!'})
            # Shooting Star
            if isup(d1) and uwk(d)>bd*2 and lwk(d)<bd*0.5:
                pats.append({'name':'Shooting Star','dir':'bear','strength':76,'desc':'Upper wick long — sellers pushed back buyers. DOWN signal!'})
            # Marubozu
            if bd/td>0.92:
                dir='bull' if isup(d) else 'bear'
                pats.append({'name':('Bull Marubozu' if isup(d) else 'Bear Marubozu'),'dir':dir,'strength':80,
                    'desc':('Pure green — buyers in full control!' if isup(d) else 'Pure red — sellers in full control!')})
        # Engulfing
        if not isup(d1) and isup(d) and d['o']<d1['c'] and d['c']>d1['o']:
            pats.append({'name':'Bullish Engulfing','dir':'bull','strength':86,'desc':'Green engulfed red — strong UP reversal!'})
        if isup(d1) and not isup(d) and d['o']>d1['c'] and d['c']<d1['o']:
            pats.append({'name':'Bearish Engulfing','dir':'bear','strength':85,'desc':'Red engulfed green — strong DOWN reversal!'})
        # Morning/Evening Star
        if not isup(d2) and body(d1)<body(d2)*0.4 and isup(d) and d['c']>(d2['o']+d2['c'])/2:
            pats.append({'name':'Morning Star','dir':'bull','strength':89,'desc':'3-candle bullish reversal — very reliable UP!'})
        if isup(d2) and body(d1)<body(d2)*0.4 and not isup(d) and d['c']<(d2['o']+d2['c'])/2:
            pats.append({'name':'Evening Star','dir':'bear','strength':88,'desc':'3-candle bearish reversal — very reliable DOWN!'})
        # Three soldiers/crows
        if all(isup(candles[-3+i]) for i in range(3)) and candles[-1]['c']>candles[-2]['c']>candles[-3]['c']:
            pats.append({'name':'Three White Soldiers','dir':'bull','strength':91,'desc':'3 green candles rising — very strong UP trend!'})
        if all(not isup(candles[-3+i]) for i in range(3)) and candles[-1]['c']<candles[-2]['c']<candles[-3]['c']:
            pats.append({'name':'Three Black Crows','dir':'bear','strength':90,'desc':'3 red candles falling — very strong DOWN trend!'})
        return pats

    # ── NEWS ─────────────────────────────────────────────────
    def news(self, symbol=""):
        if not HAS_FP:
            return self.send_json({"error":"pip install feedparser","articles":[]})
        sym_clean = symbol.replace(".NS","").replace("=X","").replace("-USD","").replace("GC=F","GOLD").upper()
        keywords  = [sym_clean] if sym_clean else []
        aliases   = {"EURUSD":["EUR","ECB"],"GBPUSD":["GBP","BOE"],"BTCUSD":["BITCOIN","CRYPTO"],
                     "GOLD":["XAU","GOLD"],"RELIANCE":["RELIANCE","RIL"]}
        for k,vs in aliases.items():
            if k in sym_clean: keywords+=vs
        articles=[]
        for src,url in NEWS_FEEDS.items():
            try:
                feed=feedparser.parse(url)
                for e in feed.entries[:5]:
                    title=( e.get('title','') or '').strip()
                    summ =_re.sub(r'<[^>]+>','',(e.get('summary','') or '')).strip()[:250]
                    if not title: continue
                    relevant=bool(keywords) and any(k in (title+summ).upper() for k in keywords)
                    articles.append({'source':src,'title':title,'summary':summ,
                        'link':e.get('link',''),'published':e.get('published',''),'relevant':relevant})
            except: pass
        articles.sort(key=lambda x:0 if x['relevant'] else 1)
        self.send_json({'articles':articles[:25],'count':len(articles)})

    # ── HELPERS ───────────────────────────────────────────────
    def send_json(self, data, status=200):
        b=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(b)))
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Cache-Control","no-cache")
        self.end_headers(); self.wfile.write(b)

    def html(self, fname):
        if not os.path.exists(fname):
            b=f"{fname} not found!".encode()
            self.send_response(404); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
        with open(fname,"rb") as f: b=f.read()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b)))
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Cache-Control","no-cache,no-store,must-revalidate")
        self.end_headers(); self.wfile.write(b)


def main():
    print("\n"+"="*55)
    print("  QX SIGNAL PRO — Backend v3")
    print("="*55)
    if not HAS_YF: print("\n  ERROR: pip install yfinance"); input(); sys.exit(1)
    print(f"\n  yfinance   : OK")
    print(f"  numpy      : {'OK' if HAS_NP else 'optional (pip install numpy)'}")
    print(f"  feedparser : {'OK' if HAS_FP else 'optional (pip install feedparser)'}")
    for f in ["quotex_signal.html","legend_oracle.html"]:
        if not os.path.exists(f): print(f"  WARNING: {f} not found!")
    print(f"\n  Quotex Signal -> http://localhost:{PORT}/quotex_signal.html")
    print(f"  Legend Oracle -> http://localhost:{PORT}")
    print(f"\n  Ctrl+C to stop\n"+"-"*55+"\n")
    try:
        s=http.server.HTTPServer(("localhost",PORT),Handler); s.serve_forever()
    except KeyboardInterrupt: print("\n  Stopped.")
    except OSError as e:
        if "10048" in str(e) or "already in use" in str(e).lower():
            print(f"\n  Port {PORT} busy! Change PORT={PORT+1}")
        input()

if __name__=="__main__": main()
