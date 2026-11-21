"""Comprobación paso a paso de la conexión con Tastytrade (solo lectura). Se detiene en el primer fallo.
Uso: python check_tasty.py   (necesita el .env con las credenciales; no imprime secretos)"""
import asyncio, sys
from dotenv import load_dotenv
load_dotenv()
import tasty


def stop(msg):
    print("✗", msg); sys.exit(1)


async def main():
    import os
    miss = [k for k in ("TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN") if not os.getenv(k)]
    if miss: stop("Faltan en .env: " + ", ".join(miss))
    print("✓ 1/5 variables presentes")
    api = tasty.Api()
    try:
        await api.token(); print("✓ 2/5 OAuth correcto (access token obtenido)")
        q = await api.get("/api-quote-tokens"); print("✓ 3/5 token de cotizaciones OK · nivel:", q.get("level"))
    except tasty.AuthError as e: stop(str(e))
    except Exception as e: stop(f"{type(e).__name__}: {str(e)[:150]}")
    p = tasty.TastyProvider(api); p.start()
    for _ in range(40):
        await asyncio.sleep(1); p.tick()
        if p.state == "error_auth": stop(p.error)
        if p.spx and p.vix and len(p.bars) >= 30: break
    else: stop("4/5 sin datos de SPX/VIX tras 40 s · estado: " + p.label)
    print(f"✓ 4/5 streaming OK · SPX {p.spx} · VIX {p.vix} · {len(p.bars)} velas · {p.label}")
    await asyncio.sleep(15); ch = p.chains()
    if not (ch[0] or ch[3]): stop("5/5 cadena sin precios (¿mercado cerrado o strikes sin cotización?)")
    print(f"✓ 5/5 cadena OK · 0DTE {len(ch[0])} contratos ({p.cm[0]['exp']}) · 1-7DTE {len(ch[3])} contratos ({p.cm[3]['exp']})")
    if ch[0]: print("  ejemplo:", ch[0][len(ch[0]) // 2])
    await p.stop()

asyncio.run(main())
