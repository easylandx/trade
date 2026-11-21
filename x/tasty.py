"""Tastytrade, SOLO LECTURA: login OAuth2 + datos en vivo por DXLink (dxFeed).

No hay ninguna función de órdenes en este archivo, ni la habrá.
Las credenciales salen únicamente de variables de entorno (.env) y nunca se escriben en logs.
"""
import asyncio, json, logging, math, os, time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import httpx
import websockets

log = logging.getLogger("tasty")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx no debe imprimir URLs/cabeceras
logging.getLogger("websockets").setLevel(logging.WARNING)

NY = ZoneInfo("America/New_York")
UA = "intraday-radar/1.0"
BASES = {"prod": "https://api.tastyworks.com", "sandbox": "https://api.cert.tastyworks.com"}
CH = 3  # canal DXLink del feed

# Campos que pedimos a DXLink (formato COMPACT: los valores llegan en este mismo orden).
FIELDS = {
    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice"],
    "Trade": ["eventType", "eventSymbol", "price", "dayVolume"],
    "Greeks": ["eventType", "eventSymbol", "delta", "gamma", "theta", "volatility"],
    "Summary": ["eventType", "eventSymbol", "openInterest"],
    "Candle": ["eventType", "eventSymbol", "time", "close", "volume"],
}


class TastyError(Exception):
    pass


def num(x):
    """Número válido o None (dxFeed manda NaN / null cuando no hay dato)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _err(r):
    try:
        j = r.json()
        e = j.get("error") or {}
        if isinstance(e, dict):
            return str(e.get("code") or e.get("message") or "")[:120]
        return str(e or j.get("error_description") or "")[:120]
    except Exception:
        return ""


def market_open(now=None):
    n = (now or datetime.now(NY)).astimezone(NY)
    return n.weekday() < 5 and dtime(9, 30) <= n.time() < dtime(16, 0)


def session_start(now=None):
    """Inicio (9:30 NY) de la última sesión: hoy si ya abrió, si no la anterior. No conoce festivos."""
    n = (now or datetime.now(NY)).astimezone(NY)
    d = n.date()
    if n.time() < dtime(9, 30):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, dtime(9, 30), NY)


class Auth:
    """OAuth2 con refresh token. El access token dura ~15 min y vive solo en memoria."""

    def __init__(self):
        self.env = os.getenv("TASTYTRADE_ENV", "prod").strip().lower()
        if self.env not in BASES:
            raise TastyError("TASTYTRADE_ENV debe ser 'prod' o 'sandbox'")
        self.base = BASES[self.env]
        self._secret = os.getenv("TASTYTRADE_CLIENT_SECRET", "").strip()
        self._refresh = os.getenv("TASTYTRADE_REFRESH_TOKEN", "").strip()
        self._cid = os.getenv("TASTYTRADE_CLIENT_ID", "").strip()
        for name, v in (("TASTYTRADE_CLIENT_SECRET", self._secret), ("TASTYTRADE_REFRESH_TOKEN", self._refresh)):
            if not v:
                raise TastyError(f"Falta {name} en el archivo .env")
        self._tok, self._exp = None, 0.0
        self._lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=15, headers={"User-Agent": UA})

    async def token(self):
        async with self._lock:
            if self._tok and time.time() < self._exp - 60:
                return self._tok
            body = {"grant_type": "refresh_token", "refresh_token": self._refresh, "client_secret": self._secret}
            if self._cid:
                body["client_id"] = self._cid
            r = await self.http.post(self.base + "/oauth/token", json=body)
            if r.status_code != 200:
                raise TastyError(f"Login rechazado (HTTP {r.status_code}) {_err(r)}")
            d = r.json()
            self._tok = d["access_token"]
            self._exp = time.time() + int(d.get("expires_in", 900))
            return self._tok

    async def get(self, path, **params):
        r = await self.http.get(self.base + path, params=params or None,
                                headers={"Authorization": "Bearer " + await self.token()})
        if r.status_code != 200:
            raise TastyError(f"GET {path} → HTTP {r.status_code} {_err(r)}")
        return r.json().get("data", {})

    async def close(self):
        await self.http.aclose()


class TastyProvider:
    """Mismos atributos que Demo: spx, vix, bars, label (vía status), tick(). Más chains()."""
    kind = "tasty"
    WINDOW = 120  # puntos de SPX a cada lado del precio para suscribir strikes

    def __init__(self):
        self.auth = Auth()
        self.vol_sym = os.getenv("VOLUME_SYMBOL", "SPY").strip().upper()
        self.off_hours = os.getenv("SIGNALS_OFF_HOURS", "0") == "1"
        self.last = {}      # símbolo -> {price, bid, ask, delta, ...}
        self.seen = {}      # símbolo -> time.monotonic() del último evento
        self.candles = {"SPX": {}, self.vol_sym: {}}
        self.struct = []    # vencimientos elegidos con sus strikes
        self.struct_ts = 0.0
        self.struct_day = None
        self.subs = set()   # símbolos de opciones suscritos
        self.connected, self.err, self.bad = False, None, 0
        self.tasks = []

    # ---------- atributos que usa el resto de la app ----------
    @property
    def spx(self):
        return self._px("SPX")

    @property
    def vix(self):
        return self._px("VIX")

    def _px(self, s):
        d = self.last.get(s, {})
        return d.get("price") or d.get("mid") or 0.0

    @property
    def bars(self):
        """[(precio, volumen)] de 1 min de la sesión. Si el volumen del SPX es 0 se usa el de SPY."""
        start = int(session_start().timestamp() * 1000)
        c, v = self.candles["SPX"], self.candles[self.vol_sym]
        ts = sorted(t for t in c if t >= start)
        proxy = sum(c[t][1] for t in ts) <= 0
        return [(c[t][0], max(1.0, (v.get(t, (0, 0))[1] if proxy else c[t][1]))) for t in ts][-400:]

    def tick(self):
        pass  # los datos llegan solos por el WebSocket

    # ---------- cadena de opciones (misma estructura que chain() de la demo) ----------
    def chains(self):
        out = {0: [], 3: []}
        for e in self.struct:
            for K, cs, ps in e["strikes"]:
                for typ, s in (("CALL", cs), ("PUT", ps)):
                    q = self.last.get(s)
                    if not q or s not in self.subs:
                        continue
                    b, a, dl = q.get("bid"), q.get("ask"), q.get("delta")
                    if not b or not a or b <= 0 or a < b or dl is None:
                        continue
                    out[e["bucket"]].append(dict(
                        dte=e["bucket"], exp=str(e["exp"]), type=typ, strike=K, delta=round(dl, 2),
                        bid=round(b, 2), ask=round(a, 2), mid=round((a + b) / 2, 2),
                        gamma=round(q.get("gamma") or 0, 4), theta=round(q.get("theta") or 0, 2),
                        vol=int(q.get("vol") or 0), oi=int(q.get("oi") or 0)))
        return out

    # ---------- estado honesto para el tablero ----------
    def _age(self, s):
        t = self.seen.get(s)
        return None if t is None else time.monotonic() - t

    def status(self):
        notes = []
        if not self.connected:
            return "off", "SIN CONEXIÓN · reintentando", [self.err or "Conectando con Tastytrade…"]
        n_opt = sum(len(v) for v in self.chains().values())
        a_spx, a_vix = self._age("SPX"), self._age("VIX")
        if self.vol_sym and sum(v[1] for v in self.candles["SPX"].values()) <= 0:
            notes.append(f"El volumen del SPX llega a 0: VWAP calculado con volumen de {self.vol_sym} (aproximación).")
        if self.err:
            notes.append(self.err)
        if self.bad:
            notes.append(f"{self.bad} mensajes de datos ilegibles (formato inesperado).")
        if not market_open():
            lbl = "MERCADO CERRADO · últimos datos"
            if a_spx is None:
                lbl = "MERCADO CERRADO · sin datos aún"
            return "closed", lbl, notes
        if a_spx is None or a_vix is None:
            return "warn", "ESPERANDO DATOS DE SPX/VIX", notes
        if a_spx > 10:
            return "warn", f"SPX RETRASADO · {int(a_spx)} s sin actualizar", notes
        if a_vix > 60:
            return "warn", f"VIX RETRASADO · {int(a_vix)} s sin actualizar", notes
        if n_opt == 0:
            return "warn", "SIN CADENA DE OPCIONES (bid/ask/delta)", notes
        return "live", "● EN VIVO · Tastytrade", notes

    def ready(self):
        lvl, label, _ = self.status()
        if lvl == "live" or (lvl == "closed" and self.off_hours and self.connected):
            return True, ""
        return False, label

    # ---------- arranque / parada ----------
    async def start(self):
        self.tasks = [asyncio.create_task(self._stream())]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        await self.auth.close()

    # ---------- cadena de vencimientos por REST ----------
    async def refresh_struct(self):
        data = await self.auth.get("/option-chains/SPX/nested")
        self.struct = self._select(data.get("items", []))
        self.struct_ts, self.struct_day, self.err = time.time(), datetime.now(NY).date(), None
        return self.struct

    @staticmethod
    def _select(items, today=None):
        """0DTE + el vencimiento de 1-7 días más cercano a 3 días. SPXW (PM) gana sobre SPX (AM)."""
        today = today or datetime.now(NY).date()
        by = {}
        for it in items:
            root = it.get("root-symbol") or it.get("underlying-symbol")
            for ex in it.get("expirations", []):
                try:
                    d = date.fromisoformat(ex["expiration-date"])
                except (KeyError, ValueError):
                    continue
                if 0 <= (d - today).days <= 7 and (d not in by or root == "SPXW"):
                    by[d] = ex
        picks = []
        if today in by:
            picks.append((0, today))
        cand = [d for d in by if 1 <= (d - today).days <= 7]
        if cand:
            picks.append((3, min(cand, key=lambda d: (abs((d - today).days - 3), (d - today).days))))
        out = []
        for bucket, d in picks:
            ks = []
            for s in by[d].get("strikes", []):
                k = num(s.get("strike-price"))
                cs, ps = s.get("call-streamer-symbol"), s.get("put-streamer-symbol")
                if k and cs and ps:
                    ks.append((k, cs, ps))
            out.append(dict(bucket=bucket, exp=d, days=(d - today).days, strikes=ks))
        return out

    def _wanted(self):
        spx = self.spx
        if not spx:
            return set()
        return {s for e in self.struct for k, cs, ps in e["strikes"]
                if abs(k - spx) <= self.WINDOW for s in (cs, ps)}

    # ---------- streaming DXLink ----------
    async def _stream(self):
        backoff = 1
        while True:
            try:
                qt = await self.auth.get("/api-quote-tokens")
                async with websockets.connect(qt["dxlink-url"], ping_interval=None, max_size=None) as ws:
                    await self._handshake(ws, qt["token"])
                    self.connected, self.err, backoff = True, None, 1
                    self.subs = set()
                    start_ms = int(session_start().timestamp() * 1000)
                    base = [{"type": t, "symbol": s} for s in ("SPX", "VIX") for t in ("Quote", "Trade")]
                    base += [{"type": "Candle", "symbol": f"{s}{{=1m}}", "fromTime": start_ms}
                             for s in ("SPX", self.vol_sym)]
                    await self._send(ws, {"type": "FEED_SUBSCRIPTION", "channel": CH, "reset": True, "add": base})
                    helpers = [asyncio.create_task(self._keepalive(ws)), asyncio.create_task(self._resync(ws))]
                    try:
                        async for raw in ws:
                            self._msg(json.loads(raw))
                    finally:
                        for h in helpers:
                            h.cancel()
                self.err = "Conexión cerrada"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # sin secretos: los mensajes de error no los incluyen
                self.err = f"{type(e).__name__}: {str(e)[:140]}"
                log.warning("Tastytrade: %s", self.err)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    @staticmethod
    async def _send(ws, msg):
        await ws.send(json.dumps(msg))

    async def _wait(self, ws, pred, timeout=10):
        async def go():
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") == "ERROR":
                    raise TastyError(f"DXLink: {m.get('error')} {str(m.get('message', ''))[:100]}")
                if pred(m):
                    return m
        return await asyncio.wait_for(go(), timeout)

    async def _handshake(self, ws, token):
        await self._send(ws, {"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                              "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        await self._wait(ws, lambda m: m.get("type") == "AUTH_STATE")
        await self._send(ws, {"type": "AUTH", "channel": 0, "token": token})
        await self._wait(ws, lambda m: m.get("type") == "AUTH_STATE" and m.get("state") == "AUTHORIZED")
        await self._send(ws, {"type": "CHANNEL_REQUEST", "channel": CH, "service": "FEED",
                              "parameters": {"contract": "AUTO"}})
        await self._wait(ws, lambda m: m.get("type") == "CHANNEL_OPENED" and m.get("channel") == CH)
        await self._send(ws, {"type": "FEED_SETUP", "channel": CH, "acceptAggregationPeriod": 0.5,
                              "acceptDataFormat": "COMPACT", "acceptEventFields": FIELDS})

    async def _keepalive(self, ws):
        t0 = time.time()
        while True:
            await asyncio.sleep(30)
            await self._send(ws, {"type": "KEEPALIVE", "channel": 0})
            if time.time() - t0 > 23 * 3600:  # el token de streaming caduca a las 24 h
                await ws.close()

    async def _resync(self, ws):
        """Mantiene la estructura de vencimientos y la ventana de strikes suscrita."""
        while True:
            try:
                if not self.struct or time.time() - self.struct_ts > 600 or self.struct_day != datetime.now(NY).date():
                    await self.refresh_struct()
            except TastyError as e:
                self.err = str(e)[:140]
            want = self._wanted()
            for key, syms in (("add", sorted(want - self.subs)), ("remove", sorted(self.subs - want))):
                for i in range(0, len(syms), 100):
                    await self._send(ws, {"type": "FEED_SUBSCRIPTION", "channel": CH, key: [
                        {"type": t, "symbol": s} for s in syms[i:i + 100]
                        for t in ("Quote", "Greeks", "Summary", "Trade")]})
            self.subs = set(want)
            await asyncio.sleep(5 if not self.subs else 20)

    # ---------- mensajes ----------
    def _msg(self, m):
        if m.get("type") != "FEED_DATA":
            return
        data = m.get("data") or []
        if data and isinstance(data[0], dict):  # formato FULL, por si acaso
            for d in data:
                self._event(d.get("eventType"), d)
            return
        i = 0
        while i + 1 < len(data):
            et, vals = data[i], data[i + 1]
            i += 2
            f = FIELDS.get(et)
            if not f or not isinstance(vals, list):
                continue
            for k in range(0, len(vals) - len(f) + 1, len(f)):
                self._event(et, dict(zip(f, vals[k:k + len(f)])))

    def _event(self, et, d):
        sym = d.get("eventSymbol")
        if d.get("eventType") != et or not isinstance(sym, str):
            self.bad += 1
            return
        if et == "Candle":
            base, t, c = sym.split("{")[0], num(d.get("time")), num(d.get("close"))
            if base in self.candles and t is not None and c is not None:
                self.candles[base][int(t)] = (c, num(d.get("volume")) or 0.0)
            return
        q = self.last.setdefault(sym, {})
        if et == "Quote":
            b, a = num(d.get("bidPrice")), num(d.get("askPrice"))
            q["bid"], q["ask"] = b, a
            if b and a:
                q["mid"] = (a + b) / 2
        elif et == "Trade":
            p = num(d.get("price"))
            if p:
                q["price"] = p
            q["vol"] = num(d.get("dayVolume")) or q.get("vol")
        elif et == "Greeks":
            for k, f in (("delta", "delta"), ("gamma", "gamma"), ("theta", "theta"), ("iv", "volatility")):
                q[k] = num(d.get(f))
        elif et == "Summary":
            q["oi"] = num(d.get("openInterest")) or q.get("oi")
        self.seen[sym] = time.monotonic()
