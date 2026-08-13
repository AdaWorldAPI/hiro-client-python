# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python client library (`hiro_graph_client`, published to PyPI as `hiro-graph-client`) for the REST and WebSocket APIs of the HIRO 7 Graph (arago / Almato AI). Requires Python >= 3.7. Pure library — there is no application entrypoint.

## Layout

This is a `src/`-layout package, but the `setup.py` lives in `src/` (not the repo root). The package itself is `src/hiro_graph_client/`. Most commands operate from `src/` or with `PYTHONPATH=src`.

## Common commands

All workflows go through the `Makefile`:

```shell
make install                 # install package + runtime deps (use PIPARGS=--user for single-user)
make test                    # install, then run pytest with PYTHONPATH=src, writes tests/test-results.xml
make pythondoc               # build Sphinx HTML docs into docs/_build/html
make dist                    # build sdist + wheel
make clean / distclean       # remove build artifacts / also uninstall
```

Run tests directly (faster iteration):

```shell
PYTHONPATH=src python -m pytest tests/unit                       # all tests
PYTHONPATH=src python -m pytest tests/unit/test_client.py        # one file
PYTHONPATH=src python -m pytest tests/unit/test_client.py::test_name   # one test
```

Note: `make test` shells out to `make install` first, which runs `pip install`. For a quick local test loop, prefer the direct `pytest` invocation above against an editable/installed package.

## Versioning

Versions are **derived from git tags**, not hardcoded. `src/version_by_git.py` runs `git describe --tags --long` during `setup.py`/`make` and writes `src/hiro_graph_client/VERSION` (read at runtime by `version.py`). Tags must look like `vX.Y.Z` (release) or `tX.Y.Z` (dev → `.devN`). No matching tag → fallback `0.0.0.dev0`. Do not edit `VERSION` by hand; bump by tagging. Publishing to PyPI only works once per version, so a new release requires a new tag.

## Architecture

Everything central lives in `clientlib.py` (~1500 lines). Two parallel class hierarchies, both rooted at `AbstractAPI` (the HTTP layer: get/post/put/patch/delete, binary streaming, header/proxy/SSL handling, retry via `backoff`, response/error parsing).

**Connection + auth side** (one per logical connection, shared across clients):
- `AbstractAPI` → `GraphConnectionHandler` — owns the `requests.Session`/connection pool, discovers and caches API endpoints and version info from the backend (`get_version`, `get_api_endpoint_of`, `get_websocket_config`).
- `GraphConnectionHandler` → `AbstractTokenApiHandler` — adds token lifecycle (`token`, `decode_token`, `refresh_token`, `revoke_token`).
- Concrete handlers: `FixedTokenApiHandler` (preset token), `EnvironmentTokenApiHandler` (reads `HIRO_TOKEN` env var), `PasswordAuthTokenApiHandler` (logs in with credentials; the only one that auto-renews on expiry, using `TokenInfo` + `apscheduler`).

**API client side** (one per HIRO API):
- `AbstractAPI` → `AuthenticatedAPIHandler` — a client that holds a reference to an `AbstractTokenApiHandler` and injects its token/endpoint into each request.
- Concrete clients, one file each, each public method ≈ one REST endpoint: `HiroGraph` (`client.py`), `HiroApp` (`appclient.py`), `HiroAuth` (`authclient.py`), `HiroAuthz` (`authzclient.py`), `HiroIam` (`iamclient.py`), `HiroKi` (`kiclient.py`), `HiroVariables` (`variablesclient.py`).

**Key design point — handler sharing:** a single `TokenApiHandler` (or `GraphConnectionHandler`) is meant to be constructed once and passed to multiple `Hiro*` clients via `api_handler=`. This shares the session, token, and version/endpoint discovery so they aren't repeated per client. When adding/changing behavior, respect this split: connection/token concerns belong on the handler hierarchy, per-API calls on the client hierarchy.

**WebSockets** (`websocketlib.py`, `eventswebsocket.py`, `actionwebsocket.py`):
- `AbstractAuthenticatedWebSocketHandler` (`websocketlib.py`) wraps `websocket-client`'s `WebSocketApp` with reconnect/backoff, token-aware auth, thread-safe start/stop, and signal handling. It also takes an `api_handler` for auth/endpoint resolution.
- Subclass and override the `on_*` callbacks: `AbstractEventsWebSocketHandler` (`on_create`/`on_update`/`on_delete`, plus `EventsFilter`) for `event-ws`; `AbstractActionWebSocketHandler` (`on_submit_action`, `on_config_changed`, with `ActionStore`/`ActionItem` tracking and `send_action_result`) for `action-ws`.
- **Shutdown rules matter and differ by caller thread:** `stop()` from another thread; `signal_stop()` from a signal handler (same thread as `run_forever()`, or it deadlocks); `restart()` likewise must not be called from the `run_forever()` thread. See the "Closing WebSockets" section of `src/README.md`.

`SSLConfig` (in `clientlib.py`) translates cert/verify settings into the `cert`/`verify` args of `requests`; it is passed to a handler and inherited by all attached clients.

## Documentation

`src/README.md` is the canonical user-facing documentation with runnable examples for every handler/client and the WebSocket shutdown semantics — read it before changing public API surface. The repo-root `README.md` only covers installation.
