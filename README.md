# MarNar Trader

Self-hosted autonomous crypto trading system for Binance Spot, running on the
MarNar Server (Raspberry Pi 4B). Private project — see `CLAUDE.md` for the
working agreement and `Context/PRODUCT-SPECIFICATION.md` for the product spec.

**Status: Phase 1 (Foundation), Sprint 1.** Testnet only — no real money touches
this system before the paper-trading gate (see `Context/PROJECT-PLAN.md`).

## Layout

- `core/` — Python 3.12 core service (FastAPI health API, exchange gateway,
  trade journal, config store). See `docs/ARCHITECTURE.md`.
- `docker-compose.yml` — production Portainer stack (arm64);
  `docker-compose.local.yml` — local-dev overrides.
- `Context/` — product docs, decisions, session state. `docs/` — team docs.
- `secrets/` — gitignored; see `secrets/README.md`.

## Development

```sh
cd core
pip install -e ".[dev]"
pytest -m "not integration"        # unit tests
ruff check .                       # lint
```

With testnet keys in the environment (`secrets/binance.env`, never committed):

```sh
pytest -m integration              # testnet round-trip
python -m trader.demo              # Sprint 1 demo: place + cancel, journaled
```

Docker (matches production image):

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
curl localhost:8000/api/health
```
