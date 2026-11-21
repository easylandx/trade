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

## Fase 2 (pendiente)
Conectar Tastytrade (SPX, VIX y cadena real) en la clase `Demo` de `app.py`: crear un proveedor con los mismos
atributos (`spx`, `vix`, `bars`, `label`, `tick()`) y la cadena real en lugar de `chain()`.
Antes de publicarlo en internet: usar HTTPS (la clave viaja en cada petición).
