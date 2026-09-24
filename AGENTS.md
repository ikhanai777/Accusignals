# Notes for AI agents (Hermes Agent and others)

- Operating instructions: `skills/accusignals/SKILL.md`. For Hermes Agent, copy
  that folder into your Hermes skills directory (by default
  `~/.hermes/skills/accusignals/`), or point the agent at it.
- Data policy: every price, volume and trade comes from the Binance public API.
  There's no synthetic or sample data in the product, and tests that need
  market data skip when Binance is unreachable. Never make up signals or
  metrics. If a command exits with code 2, report that Binance is unreachable.
- The tool only produces signals. It never places orders.
- Development: `pytest` runs the tests. Real-data tests download BTCUSDT 5m
  history once into `tests/.cache/`.
