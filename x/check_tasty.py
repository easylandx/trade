"""Prueba la conexión con Tastytrade paso a paso (solo lectura). Se detiene en el primer fallo.
Uso:  python check_tasty.py
"""
import asyncio, sys
from dotenv import load_dotenv

load_dotenv()
from tasty import Auth, TastyError, TastyProvider, market_open


def ok(msg): print("  ✓", msg)
def fail(msg): print("  ✗", msg); sys.exit(1)


async def main():
    print("1/5 Login OAuth (credenciales del .env)")
    try:
        auth = Auth()
        await auth.token()
    except TastyError as e:
        fail(f"{e}\n    Revisa TASTYTRADE_CLIENT_SECRET, TASTYTRADE_REFRESH_TOKEN y TASTYTRADE_ENV "
             "(sandbox y prod son credenciales distintas).")
    ok(f"Token de acceso obtenido ({auth.env})")

    print("2/5 Cuenta")
    try:
        items = (await auth.get("/customers/me/accounts")).get("items", [])
    except TastyError as e:
        fail(str(e))
    if not items:
        fail("La API no devuelve ninguna cuenta para estas credenciales.")
    ok(f"{len(items)} cuenta(s) accesible(s)")

    print("3/5 Token de streaming de datos")
    try:
        qt = await auth.get("/api-quote-tokens")
    except TastyError as e:
        fail(f"{e}\n    Hace falta una cuenta con fondos/datos de mercado activos.")
    ok("Token de streaming recibido (nivel: %s)" % qt.get("level"))

    print("4/5 Cadena de opciones SPX")
    prov = TastyProvider()
    try:
        struct = await prov.refresh_struct()
    except TastyError as e:
        fail(str(e))
    if not struct:
        fail("No hay vencimientos 0-7 días en la respuesta.")
    for e in struct:
        ok(f"{'0DTE' if e['bucket'] == 0 else '1-7 DTE'}: vence {e['exp']} ({e['days']} días), {len(e['strikes'])} strikes")

    print("5/5 Streaming en vivo (20 s)" + ("" if market_open() else "  [mercado cerrado: pueden no llegar ticks nuevos]"))
    await prov.start()
    await asyncio.sleep(20)
    lvl, label, notes = prov.status()
    n = {k: len(v) for k, v in prov.chains().items()}
    print(f"    SPX={prov.spx}  VIX={prov.vix}  velas 1m={len(prov.bars)}  contratos 0DTE={n[0]} 1-7DTE={n[3]}")
    print(f"    Estado: {label}")
    for x in notes:
        print("    ·", x)
    await prov.stop()
    if not prov.connected or not prov.spx:
        fail("No llegaron datos de SPX. Si el mercado está abierto, revisa que la cuenta tenga datos en tiempo real.")
    ok("Todo correcto")


asyncio.run(main())
