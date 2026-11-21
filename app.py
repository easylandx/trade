"""Intraday Radar AI: radar de señales SPX (solo lectura + simulación)."""
import asyncio, json, math, os, random, secrets, sys, time
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

load_dotenv()
USER = os.getenv("APP_USER", "radar")
PASS = os.getenv("APP_PASS", "")  # vacío = sin login (solo para uso local en casa)
SOURCE = os.getenv("DATA_SOURCE", "demo")
MIN_SCORE = int(os.getenv("MIN_SCORE", "75"))
HERE = Path(__file__).parent
TRADES = HERE / "trades.json"


class Demo:
    """Datos simulados (1 tick = 1 vela). Fase 2: sustituir por Tastytrade."""
    label = "DEMO · datos simulados"

    def __init__(self):
        self.spx, self.vix, self.bars = 5900.0, 16.0, []
        for _ in range(150):
            self.tick()

    def tick(self):
        self.spx *= 1 + random.gauss(0.00003, 0.0004)
        self.vix = max(10, self.vix + random.gauss(0, 0.05))
        self.bars = (self.bars + [(self.spx, random.randint(800, 1500))])[-400:]


def make_provider():
    if SOURCE == "demo":
        return Demo()
    sys.exit("DATA_SOURCE=tasty aún no está implementado (fase 2, ver README).")


def ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def indicators(bars):
    p = [b[0] for b in bars]
    vw = sum(b[0] * b[1] for b in bars) / sum(b[1] for b in bars)
    return p, vw, ema(p, 9), ema(p, 21)


def find_signals(bars, vix):
    p, vw, e9, e21 = indicators(bars)
    px, up = p[-1], ema(p, 9) > ema(p, 21)
    hi, lo = max(p[-60:-1]), min(p[-60:-1])
    raw = []

    def add(name, side, base, why):
        aligned = (side == "CALL") == up
        raw.append(dict(strategy=name, side=side, why=why,
                        score=base + (15 if aligned else -15) + (10 if vix < 20 else 0)))

    if px > hi: add("Ruptura", "CALL", 60, "Rompe el máximo de la última hora")
    if px < lo: add("Ruptura", "PUT", 60, "Rompe el mínimo de la última hora")
    if min(p[-5:]) <= vw * 1.0003 and px > vw and p[-1] > p[-2]:
        add("Rebote VWAP", "CALL", 60, "Rebota en el VWAP al alza")
    if max(p[-5:]) >= vw * 0.9997 and px < vw and p[-1] < p[-2]:
        add("Rebote VWAP", "PUT", 60, "Rebota en el VWAP a la baja")
    if up and px > e9 > vw and p[-1] > p[-2]:
        add("Tendencia", "CALL", 55, "EMA 9 sobre EMA 21 y precio sobre VWAP")
    if not up and px < e9 < vw and p[-1] < p[-2]:
        add("Tendencia", "PUT", 55, "EMA 9 bajo EMA 21 y precio bajo VWAP")
    for s in raw:  # confluencia: cada estrategia que coincide suma
        s["score"] = min(100, s["score"] + 10 * (sum(r["side"] == s["side"] for r in raw) - 1))
    return [s for s in raw if s["score"] >= MIN_SCORE]


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def chain(S, vix, dte):
    """Cadena sintética (Black-Scholes). Fase 2: cadena real de Tastytrade."""
    sig, T = vix / 100, max(dte, 0.25) / 252
    out, base = [], int(S // 5 * 5)
    for K in range(base - 100, base + 105, 5):
        d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
        d2 = d1 - sig * math.sqrt(T)
        for typ in ("CALL", "PUT"):
            call = typ == "CALL"
            pr = S * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - S * ncdf(-d1)
            h = max(0.05, pr * 0.03)
            out.append(dict(dte=dte, type=typ, strike=K,
                            delta=round(ncdf(d1) if call else ncdf(d1) - 1, 2),
                            bid=round(max(pr - h, 0.05), 2), ask=round(pr + h, 2)))
    return out


def pick(ch, side, target=0.30):
    ok = [o for o in ch if o["type"] == side and (o["ask"] - o["bid"]) / ((o["ask"] + o["bid"]) / 2) < 0.10]
    return min(ok, key=lambda o: abs(abs(o["delta"]) - target), default=None)


trades = json.loads(TRADES.read_text()) if TRADES.exists() else []
last_open = {}


def simulate(sigs, chains):
    now, changed = time.time(), False
    for t in trades:
        if t["status"] != "open":
            continue
        o = next((c for c in chains[t["dte"]] if c["type"] == t["type"] and c["strike"] == t["strike"]), None)
        if not o:
            continue
        pnl = o["bid"] / t["entry"] - 1
        t["pnl"] = round(pnl * 100, 1)
        if pnl >= 0.5 or pnl <= -0.3 or now - t["ts"] > 1800:
            t["status"], changed = "cerrada", True
    for s in sigs:
        key = (s["strategy"], s["side"])
        if now - last_open.get(key, 0) < 300:
            continue
        for dte in chains:
            o = pick(chains[dte], s["side"])
            if o:
                trades.append(dict(ts=now, hora=time.strftime("%H:%M:%S"), dte=dte, strategy=s["strategy"],
                                   type=o["type"], strike=o["strike"], entry=o["ask"], pnl=0.0,
                                   status="open", score=s["score"]))
                changed = True
        last_open[key] = now
    if changed:
        TRADES.write_text(json.dumps(trades[-500:]))


def stats():
    done = [t for t in trades if t["status"] != "open"]
    if not done:
        return dict(n=0, win=0, avg=0)
    return dict(n=len(done), win=round(100 * sum(t["pnl"] > 0 for t in done) / len(done)),
                avg=round(sum(t["pnl"] for t in done) / len(done), 1))


prov, cache = make_provider(), {}


async def loop():
    while True:
        prov.tick()
        chains = {0: chain(prov.spx, prov.vix, 0), 3: chain(prov.spx, prov.vix, 3)}
        sigs = find_signals(prov.bars, prov.vix)
        simulate(sigs, chains)
        p, vw, e9, e21 = indicators(prov.bars)
        cache.update(source=prov.label, spx=round(prov.spx, 2), vix=round(prov.vix, 2), vwap=round(vw, 2),
                     ema9=round(e9, 2), ema21=round(e21, 2), prices=[round(x, 2) for x in p[-120:]],
                     signals=sigs, min_score=MIN_SCORE, stats=stats(), trades=trades[-25:][::-1],
                     radar={"0DTE": [pick(chains[0], "CALL"), pick(chains[0], "PUT")],
                            "1-7 DTE": [pick(chains[3], "CALL"), pick(chains[3], "PUT")]})
        await asyncio.sleep(1)


@asynccontextmanager
async def life(_):
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(lifespan=life)
sec = HTTPBasic(auto_error=False)


def auth(c: HTTPBasicCredentials = Depends(sec)):
    if not PASS:
        return
    if not c or not (secrets.compare_digest(c.username, USER) and secrets.compare_digest(c.password, PASS)):
        raise HTTPException(401, "Usuario o clave incorrectos", headers={"WWW-Authenticate": "Basic"})


@app.get("/api/state")
def state(_=Depends(auth)):
    return cache


@app.get("/")
def index(_=Depends(auth)):
    return FileResponse(HERE / "index.html")
