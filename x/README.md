# Intraday Radar AI

Radar de señales para opciones SPX (0DTE y 1-7 DTE). **Solo lectura y simulación: no envía órdenes.**

## Archivos
- `app.py` – servidor FastAPI, señales, simulador (lógica de señales sin cambios)
- `tasty.py` – conexión Tastytrade (OAuth2 + streaming DXLink). Sin función de órdenes
- `check_tasty.py` – prueba la conexión paso a paso
- `index.html`, `manifest.webmanifest`, `sw.js`, iconos – tablero y PWA

## Conseguir las credenciales de Tastytrade (OAuth2)
El acceso con usuario y contraseña ya no existe. En my.tastytrade.com:
1. **Manage → My Profile → API → OAuth Applications → + New OAuth client** → guarda el **client secret** (solo se muestra una vez).
2. En esa aplicación: **Manage → Create Grant** → guarda el **refresh token** (no caduca).
3. Pon ambos en `.env` (copia de `.env.example`). Solo funciona con la cuenta que creó la aplicación.

## Arrancar
1. `pip install -r requirements.txt`
2. Copia `.env.example` a `.env`, rellena `TASTYTRADE_CLIENT_SECRET` y `TASTYTRADE_REFRESH_TOKEN`
3. Prueba la conexión: `python check_tasty.py` (se detiene en el primer fallo)
4. Pon `DATA_SOURCE=tasty` en `.env` (con `demo` usa datos simulados)
5. `uvicorn app:app --host 127.0.0.1 --port 8000` y abre http://localhost:8000
   (móvil en el mismo wifi: `--host 0.0.0.0` y la IP del ordenador; usa `APP_PASS` si no es solo en casa)

## Qué avisa el tablero (insignia de estado)
EN VIVO · SPX/VIX RETRASADO · ESPERANDO DATOS · SIN CADENA DE OPCIONES · MERCADO CERRADO · SIN CONEXIÓN.
Si algo no está en vivo, **las señales se pausan** y la razón aparece bajo la cabecera. Fuera de horario puedes
probar con `SIGNALS_OFF_HOURS=1` en `.env`.

## Cómo se obtienen los datos
- SPX y VIX: streaming (Quote/Trade). Velas de 1 min: evento Candle desde las 9:30 (NY).
- Volumen: si el SPX llega con volumen 0 se usa el de SPY (`VOLUME_SYMBOL`) y el tablero lo indica.
- Cadena: `/option-chains/SPX/nested` (0DTE y el vencimiento de 1-7 días más cercano a 3), ±120 puntos de strikes;
  bid/ask, delta, gamma, theta, volumen y open interest por streaming.

## Pendiente (siguiente fase)
Estrategias detalladas (niveles del día anterior, retesteo, 5/15 min, filtros de hora y liquidez): se ajustarán con las reglas exactas.
Para instalar como app en el móvil hace falta HTTPS (o localhost).
