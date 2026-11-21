"""Proveedor Tastytrade para Intraday Radar AI.

ESTRICTAMENTE SOLO LECTURA: OAuth2 (scope `read`), REST con GET y streaming DXLink.
No existe ninguna función para enviar órdenes. Los secretos solo se leen del entorno (.env)
y nunca se imprimen.
"""
import asyncio, json, math, os, time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import websockets

ET = ZoneInfo("America/New_York")
UA = "intraday-radar-ai/1.0"
WINDOW = 100          # strikes a ±100 puntos del precio (igual que la cadena simulada)
RECENTER = 25         # se recentra la suscripción si el precio se mueve más de esto
FIELDS = {
    "Quote": ["eventType", "eventSymbol", "bidPrice", "askPrice"],
    "Trade": ["eventType", "eventSymbol", "price", "dayVolume"],
    "Greeks": ["eventType", "eventSymbol", "delta"],
    "Summary": ["eventType", "eventSymbol", "openInterest", "dayClosePrice", "prevDayClosePrice"],
    "Candle": ["eventType", "eventSymbol", "time", "open", "high", "low", "close", "volume"],
}


class AuthError(Exception):
    """Error fatal de credenciales/permisos: no se reintenta."""


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def market_open(now=None):
    n = (now or datetime.now(ET)).astimezone(ET)
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def _err(r):
    try:
        return json.dumps(r.json().get("error", ""))[:160]
    except Exception:
        return ""


class Api:
    """Cliente REST mínimo. Solo GET + el canje del refresh token en /oauth/token."""

    def __init__(self):
        self.base = os.getenv("TASTYTRADE_BASE_URL", "https://api.tastyworks.com").rstrip("/")
        self.secret = os.environ["TASTYTRADE_CLIENT_SECRET"]
        self.refresh = os.environ["TASTYTRADE_REFRESH_TOKEN"]
        self.client_id = os.getenv("TASTYTRADE_CLIENT_ID", "")
        self.http = httpx.AsyncClient(timeout=20, headers={"User-Agent": UA, "Accept": "application/json"})
        self.tok, self.exp = None, 0.0

    async def token(self):
        if self.tok and time.time() < self.exp - 60:
            return self.tok
        body = {"grant_type": "refresh_token", "refresh_token": self.refresh, "client_secret": self.secret}
        if self.client_id:
            body["client_id"] = self.client_id
        r = await self.http.post(self.base + "/oauth/token", json=body)
        if r.status_code != 200:
            raise AuthError(f"OAuth rechazado (HTTP {r.status_code} {_err(r)}). Revisa .env y que el entorno coincida con TASTYTRADE_BASE_URL")
        d = r.json()
        self.tok, self.exp = d["access_token"], time.time() + float(d.get("expires_in", 900))
        return self.tok

    async def get(self, path, _retry=True):
        r = await self.http.get(self.base + path, headers={"Authorization": "Bearer " + await self.token()})
        if r.status_code == 401 and _retry:
            self.tok = None
            return await self.get(path, False)
        if r.status_code in (401, 403):
            raise AuthError(f"Sin permiso en {path} (HTTP {r.status_code}); el grant necesita el scope read")
        r.raise_for_status()
        return r.json()["data"]

    async def close(self):
        await self.http.aclose()


class TastyProvider:
    """Mismos atributos que Demo: spx, vix, bars, label, tick(). Más chains() con la cadena real."""

    def __init__(self, api=None):
        self.api = api or Api()
        self.spx = self.vix = None
        self.bars = []
        self.state, self.error, self.conn = "conectando", "", False
        self.px, self.qmid, self.close = {}, {}, {}
        self.spx_c, self.spy_v = {}, {}          # velas 1m: tiempo(ms) -> (cierre, vol) / volumen SPY
        self.opts, self.sub = {}, set()
        self.cm = {}                             # {0: {...}, 3: {...}} expiraciones elegidas
        self.center, self.chain_ts, self.chain_day = None, 0.0, None
        self.last_spx, self.last_candle, self.level = 0.0, None, ""
        self.backoff, self.auth_tries = 5, 0
        self.fields, self.task, self.ws = dict(FIELDS), None, None

    # ---------- ciclo de vida ----------
    def start(self):
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
        await self.api.close()

    async def _run(self):
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except AuthError as e:
                self.state, self.error, self.conn = "error_auth", str(e), False
                print("[tasty] ERROR de autenticación:", e, "· no se reintenta")
                return
            except Exception as e:
                self.state, self.error, self.conn = "reconectando", type(e).__name__, False
                print(f"[tasty] conexión perdida ({type(e).__name__}: {str(e)[:120]}); reintento en {self.backoff}s")
            await asyncio.sleep(self.backoff)
            self.backoff = min(self.backoff * 2, 60)

    async def _session(self):
        q = await self.api.get("/api-quote-tokens")
        url, tok, self.level = q["dxlink-url"], q["token"], q.get("level", "")
        await self._load_chains()
        self.auth_tries, self.fields = 0, dict(FIELDS)
        async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
            self.ws = ws
            await self._send({"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                              "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
            maint = asyncio.create_task(self._maint())
            try:
                async for raw in ws:
                    await self._on_msg(json.loads(raw), tok)
                    if maint.done():
                        maint.result()
            finally:
                maint.cancel()
                self.conn, self.ws, self.sub, self.center = False, None, set(), None
        raise ConnectionError("stream cerrado")

    async def _send(self, m):
        if self.ws:
            await self.ws.send(json.dumps(m))

    async def _on_msg(self, m, tok):
        t = m.get("type")
        if t == "AUTH_STATE":
            if m.get("state") == "UNAUTHORIZED":
                self.auth_tries += 1
                if self.auth_tries > 1:
                    raise AuthError("DXLink rechazó el token de cotizaciones")
                await self._send({"type": "AUTH", "channel": 0, "token": tok})
            elif m.get("state") == "AUTHORIZED":
                await self._send({"type": "CHANNEL_REQUEST", "channel": 1, "service": "FEED",
                                  "parameters": {"contract": "AUTO"}})
        elif t == "CHANNEL_OPENED":
            await self._send({"type": "FEED_SETUP", "channel": 1, "acceptAggregationPeriod": 0.5,
                              "acceptDataFormat": "COMPACT", "acceptEventFields": FIELDS})
            from_ms = int((time.time() - 3 * 86400) * 1000)
            add = []
            for s in ("SPX", "VIX"):
                add += [{"type": k, "symbol": s} for k in ("Trade", "Quote", "Summary")]
            add += [{"type": "Candle", "symbol": s + "{=1m}", "fromTime": from_ms} for s in ("SPX", "SPY")]
            await self._send({"type": "FEED_SUBSCRIPTION", "channel": 1, "reset": True, "add": add})
            self.conn, self.state, self.error, self.backoff = True, "ok", "", 5
        elif t == "FEED_CONFIG" and m.get("eventFields"):
            self.fields.update(m["eventFields"])
        elif t == "FEED_DATA":
            self._feed(m.get("data"))
        elif t == "ERROR":
            raise ConnectionError(f"DXLink: {m.get('error')} {str(m.get('message', ''))[:100]}")

    async def _maint(self):
        last_ka = 0.0
        while True:
            if time.time() - last_ka > 25:
                await self._send({"type": "KEEPALIVE", "channel": 0})
                last_ka = time.time()
            if self.conn:
                today = datetime.now(ET).date()
                if time.time() - self.chain_ts > 1800 or today != self.chain_day:
                    try:
                        await self._load_chains()
                    except AuthError:
                        raise
                    except Exception as e:
                        print("[tasty] no se pudo refrescar la cadena:", type(e).__name__)
                await self._resubscribe()
            await asyncio.sleep(3)

    # ---------- cadena de opciones ----------
    async def _load_chains(self):
        items = (await self.api.get("/option-chains/SPX/nested"))["items"]
        today = datetime.now(ET).date()
        exps = {}
        for it in items:
            root = it.get("root-symbol", "")
            for ex in it.get("expirations", []):
                dt = ex.get("expiration-date")
                d = (date.fromisoformat(dt) - today).days
                if d < 0 or (dt in exps and root != "SPXW"):
                    continue
                exps[dt] = (d, root, ex.get("strikes", []))
        if not exps:
            raise ConnectionError("cadena SPX vacía")
        by_d = sorted((v[0], k) for k, v in exps.items())
        d0, dt0 = by_d[0]
        cand = [x for x in by_d if 1 <= x[0] <= 7 and x[1] != dt0] or [x for x in by_d if x[1] != dt0] or by_d
        d3, dt3 = min(cand, key=lambda x: (abs(x[0] - 3), x[0]))
        cm = {}
        for key, (d, dt) in ((0, (d0, dt0)), (3, (d3, dt3))):
            rows = []
            for s in exps[dt][2]:
                k = num(s.get("strike-price", s.get("strike")))
                if k is None or k % 5:
                    continue
                rows.append((int(k), s.get("call-streamer-symbol"), s.get("put-streamer-symbol")))
            cm[key] = dict(exp=dt, d=d, strikes=rows)
        self.cm, self.center = cm, None
        self.chain_ts, self.chain_day = time.time(), today

    def _wanted(self):
        out = set()
        for cm in self.cm.values():
            for k, cs, ps in cm["strikes"]:
                if abs(k - self.center) <= WINDOW:
                    out |= {s for s in (cs, ps) if s}
        return out

    async def _resubscribe(self):
        if self.spx is None:
            return
        if self.center is not None and abs(self.spx - self.center) <= RECENTER:
            return
        self.center = self.spx
        want = self._wanted()
        for key, syms in (("add", sorted(want - self.sub)), ("remove", sorted(self.sub - want))):
            for i in range(0, len(syms), 60):
                ents = [{"type": k, "symbol": s} for s in syms[i:i + 60] for k in ("Quote", "Greeks", "Summary", "Trade")]
                await self._send({"type": "FEED_SUBSCRIPTION", "channel": 1, key: ents})
        for s in self.sub - want:
            self.opts.pop(s, None)
        self.sub = want

    # ---------- eventos ----------
    def _feed(self, data):
        if not data:
            return
        if isinstance(data[0], str):
            data = [data]
        for et, flat in data:
            names = self.fields.get(et)
            if not names:
                continue
            n = len(names)
            for i in range(0, len(flat) - n + 1, n):
                self._event(et, dict(zip(names, flat[i:i + n])))

    def _event(self, et, e):
        sym = e.get("eventSymbol") or ""
        if et == "Candle":
            t, c = e.get("time"), num(e.get("close"))
            if t is None or c is None:
                return
            if sym.startswith("SPX"):
                self.spx_c[int(t)] = (c, num(e.get("volume")) or 0)
                self.last_candle, self.last_spx = int(t), time.monotonic()
            elif sym.startswith("SPY"):
                self.spy_v[int(t)] = num(e.get("volume")) or 0
        elif sym in ("SPX", "VIX"):
            if et == "Trade" and num(e.get("price")):
                self.px[sym] = num(e["price"])
            elif et == "Quote":
                b, a = num(e.get("bidPrice")), num(e.get("askPrice"))
                if b and a:
                    self.qmid[sym] = (a + b) / 2
            elif et == "Summary":
                self.close[sym] = num(e.get("dayClosePrice")) or num(e.get("prevDayClosePrice")) or self.close.get(sym)
            if sym == "SPX":
                self.last_spx = time.monotonic()
        elif sym.startswith("."):
            o = self.opts.setdefault(sym, {})
            if et == "Quote":
                o["bid"], o["ask"] = num(e.get("bidPrice")), num(e.get("askPrice"))
            elif et == "Greeks":
                o["delta"] = num(e.get("delta"))
            elif et == "Summary":
                o["oi"] = num(e.get("openInterest"))
            elif et == "Trade":
                o["volume"] = num(e.get("dayVolume"))

    # ---------- interfaz igual a Demo ----------
    def _bars(self):
        ts = sorted(self.spx_c)
        if not ts:
            return []
        day = lambda t: datetime.fromtimestamp(t / 1000, ET).date()
        sess = [t for t in ts if day(t) == day(ts[-1])]
        use = sess if len(sess) >= 30 else ts[-400:]   # sesión actual; si acaba de abrir, últimas 400 velas
        return [(self.spx_c[t][0], self.spy_v.get(t) or self.spx_c[t][1] or 1) for t in use[-400:]]

    def tick(self):
        self.bars = self._bars()
        last = self.bars[-1][0] if self.bars else None
        self.spx = self.px.get("SPX") or self.qmid.get("SPX") or last or self.close.get("SPX")
        self.vix = self.px.get("VIX") or self.qmid.get("VIX") or self.close.get("VIX")

    def chains(self):
        out = {0: [], 3: []}
        if self.spx is None:
            return out
        for key, cm in self.cm.items():
            for k, cs, ps in cm["strikes"]:
                if abs(k - self.spx) > WINDOW:
                    continue
                for typ, sym in (("CALL", cs), ("PUT", ps)):
                    o = self.opts.get(sym) or {}
                    b, a, dl = o.get("bid"), o.get("ask"), o.get("delta")
                    if not b or not a or dl is None or a < b:
                        continue
                    out[key].append(dict(dte=key, type=typ, strike=k, delta=round(dl, 2), bid=round(b, 2),
                                         ask=round(a, 2), volume=int(o.get("volume") or 0), oi=int(o.get("oi") or 0),
                                         exp=cm["exp"], real_dte=cm["d"]))
        return out

    @property
    def label(self):
        if self.state == "error_auth":
            return f"TASTYTRADE · ERROR de acceso: {self.error}"
        if not self.conn:
            return "TASTYTRADE · " + ("reconectando…" if self.state == "reconectando" else "conectando…") + (f" ({self.error})" if self.error else "")
        miss = [n for n, v in (("SPX", self.spx), ("VIX", self.vix)) if v is None]
        if miss or len(self.bars) < 30:
            return "TASTYTRADE · cargando datos (" + (", ".join(miss) or f"velas {len(self.bars)}/30") + ")"
        if not market_open():
            return "TASTYTRADE · MERCADO CERRADO · mostrando el último cierre · volumen: SPY"
        warns = []
        age = time.monotonic() - self.last_spx
        lag = (time.time() * 1000 - self.last_candle) / 1000 if self.last_candle else None
        if age > 30:
            warns.append(f"SIN DATOS NUEVOS de SPX hace {int(age)} s")
        elif lag is not None and lag > 180:
            warns.append(f"RETRASADO ~{int(lag // 60)} min")
        got = sum(len(v) for v in self.chains().values())
        if got < 10:
            warns.append(f"cadena parcial ({got} contratos con precio)")
        return "TASTYTRADE · " + ("EN VIVO" if not warns else "⚠ " + " · ".join(warns)) + " · volumen: SPY"
