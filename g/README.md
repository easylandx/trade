# Intraday Radar AI

Radar de señales para opciones SPX (0DTE y 1-7 DTE). Solo lectura y simulación: no ejecuta órdenes.

## Arrancar
1. `pip install -r requirements.txt`
2. Copia `.env.example` a `.env` (la clave `APP_PASS` es opcional; déjala vacía para uso solo en casa).
3. `uvicorn app:app --host 127.0.0.1 --port 8000` (solo este ordenador; usa `0.0.0.0` para verlo desde el móvil en el mismo wifi)
4. Abre http://localhost:8000 (desde el móvil: la IP del ordenador, mismo wifi).

## Estado actual
- Funciona con **datos simulados** (DEMO) para ver el tablero, las señales y el simulador.
- Señales: Ruptura, Rebote VWAP y Tendencia, con confluencia y umbral `MIN_SCORE`.
- Simulador: entra al ask, sale al bid (+50% objetivo, -30% stop, 30 min máx.). Diario en `trades.json`.

## Fase 2: Tastytrade (solo lectura)
Autenticación: **OAuth2 con "personal grant"** (el login con contraseña ya no existe). Datos: REST + streaming DXLink.

1. En my.tastytrade.com > Manage > My Profile > API > OAuth Applications crea una aplicación con el scope **`read`** únicamente
   y pulsa Manage > Create Grant.
2. En tu `.env` (copia de `.env.example`) rellena `TASTYTRADE_CLIENT_SECRET` y `TASTYTRADE_REFRESH_TOKEN`
   (y `TASTYTRADE_BASE_URL`: producción = datos reales; `https://api.cert.tastyworks.com` = sandbox). Pon `DATA_SOURCE=tasty`.
3. `pip install -r requirements.txt` y `python check_tasty.py` (prueba 5 pasos y se detiene si algo falla).
4. Arranca con `uvicorn app:app --host 127.0.0.1 --port 8000`.

Detalles:
- **Volumen:** SPX no trae volumen útil; el VWAP usa el volumen de **SPY** (velas de 1 minuto) como aproximación.
- **Velas:** SPX 1 min; VWAP de la sesión actual (si acaba de abrir y hay <30 velas, se usan las últimas 400).
- **Cadena:** SPX 0DTE (o la siguiente sesión si hoy no hay) y la expiración más cercana a 3 días dentro de 1-7 DTE,
  strikes de 5 en 5 a ±100 puntos. Mismos campos que antes más `volume`, `oi`, `exp`, `real_dte`.
- **Badge:** EN VIVO · ⚠ RETRASADO · SIN DATOS NUEVOS · cadena parcial · MERCADO CERRADO (muestra el último cierre) ·
  reconectando · ERROR de acceso (las credenciales erróneas no se reintentan).
- **Solo lectura:** el cliente solo hace GET (y el canje del token); no hay código de órdenes y el scope `trade` no se pide.
- Antes de publicarlo en internet: usar HTTPS y `APP_PASS`.
