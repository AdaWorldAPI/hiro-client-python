# v5.4.0

* [feature] HTTP status 429 (Too Many Requests) from the HIRO rate limiter is handled. A rejected request is repeated
  after waiting for the `Retry-After` header (with jitter; 1s, 2s, 4s... when the header is missing). This is
  independent from the `max_tries` retries for 5xx errors. Configure it with the new parameters
  `rate_limit_max_retries` (default 3, 0 disables) and `rate_limit_max_wait` (default 60 seconds per wait, 0 means
  never wait). When the retries are used up or the demanded wait exceeds the cap, the new `HiroRateLimitError` (a
  subclass of `requests.exceptions.HTTPError` with `retry_after`, `policy` and `ratelimit`) is raised. Streaming
  downloads are only repeated while no data has been received. This policy is a deliberate choice for a library: other
  components either hand 429 to the caller or retry without limit.
* [feature] A 429 while opening a WebSocket no longer terminates it. The reconnect waits at least `Retry-After` (with
  jitter).
* [feature] Action WebSocket: when the ws-proxy rejects a frame with `{"error": {"code": 429, "retryAfterMs": ...},
  "id": ...}`, a stored `sendActionResult` is sent again after `retryAfterMs` (with jitter) until it is acknowledged or
  expired. Previously this was answered with a `negativeAcknowledged` (400) and the result was never re-sent.

# v5.3.5

* [bugfix] An unset `max_tries` was stored as `None`, which `backoff` interprets as "retry forever". Requests to an
  unreachable host therefore retried endlessly and never surfaced the connect timeout added in v5.3.4. `max_tries` now
  falls back to its documented default of `2`.
* [bugfix] Timeouts are now raised as `HiroTimeoutError`, which inherits from both `requests.exceptions.Timeout` and
  the builtin `TimeoutError`. Previously the exception class depended on when the timeout occurred, and a timeout while
  reading the response body surfaced as a plain `requests.exceptions.ConnectionError` - so it was not caught by
  `except TimeoutError` nor by `except requests.exceptions.Timeout` and crashed the calling script. The subclasses
  `HiroConnectTimeoutError`, `HiroReadTimeoutError` and `HiroBodyTimeoutError` keep the original `requests` class as an
  additional base class, so existing error handling keeps working unchanged. See "Timeouts and error handling" in
  `src/README.md`.

# v5.3.4

* [bugfix] Connection establishment now honors a dedicated connect timeout. The `timeout` parameter accepts a
  `(connect, read)` tuple (or a scalar, which is combined with the default connect timeout). The default changed from
  `600` to `(10, 600)` so an unreachable host no longer blocks for the full read timeout.

# v5.3.3

* Update setuptools.
* Fix `escaped_query`.

# v5.3.2

* Point to https://core.engine.datagroup.de instead of https://core.arago.co.
* Add `HiroGraph.escaped_query` method.

# v5.3.0

* Decode any token via static `decode_token_ext()`.

Changes for /api/auth/6.6:

* Ability to revoke a token.
* Recognize `access_token` and `_TOKEN` in token results.
* Remove obsolete method `fresh()` from class TokenInfo because each token refresh issues a new refresh_token by default
  now.

# v5.2.5

* [bugfix] Calculation within *clientlib.TokenInfo.expired()* now returns *False* as expected when no *expires_at* 
  is available for a token. Previous versions would raise an exception.

# v5.2.4

* Merge headers in AbstractAPI instead of replacing them.

# v5.2.3

* Remove accept_all_certs for good. Directly use the flag in SSLConfig.

# v5.2.2

* Debugged python code documentation.
* Fixed copyright notices.

# v5.2.1

* Update because of wrong documentation on https://pypi.org/project/hiro-graph-client/

# v5.2.0

* Separate connection sessions from token handlers. Introducing class `GraphConnectonHandler` which can be shared
  between `TokenApiHandler`s and API-Classes.
* Updated documentation.

# v5.1.0

* Use connection pooling via requests.Session.
    * Set `pool_connections=1`, `pool_maxsize=10` by default (the latter can be changed).
    * Option `pool_block` to block any connections that would exceed the `pool_maxsize`.
* Removed obsolete `accept_all_certs()`.
* Fixed pytest in Makefile target.
* Added information about `requests.Session` and the `pool_maxsize` to documentation.

# v5.0.0

* Externalize `batchclient.py` as its own project at https://github.com/arago/hiro-batch-client-python.

# v4.9.0

* Added batch handling of issues. `handle_vertices_combined` recognizes `_issue_data` now for issues directly linked to
  vertices.
* Updated documentation.

# v4.8.0

* Refactoring of `websocketlib.py`:

    * Removed unnecessary extra thread.
    * Debugged shutdown of WebSockets via signals and threads.
    * Updated documentation:
        * On signal handling.
        * On clean shutdown of WebSockets.
        * Adjusted the example code for WebSockets.


* Allow `-` or `_` as build number separator on test tags, i.e. `t4.8.0_0` and `t4.8.0-0` are handled identical now.

# v4.7.5

* [budgfix] Handle `self.submitStore` and `self.resultStore` properly on shutdown in AbstractActionWebSocketHandler.
  They might not have been initialized at all when startup fails.

# v4.7.4

* [bugfix] Catch 401 when using PasswordAuthTokenHandler and refresh_token. Raise TokenUnauthorizedError when this
  happens to trigger a retry.

# v4.7.3

* Refactoring of `HiroGraphBatch`. Removed obsolete methods.

# v4.7.2

* Fixed bad call of patch() methods in IAM API.

# v4.7.1

* Updated some code documentation in `HiroGraph` that became obsolete.

* Updated graph queries in `HiroGraph`:

    * For vertex and gremlin queries: Skip unnecessary fields in query payload.
    * Add parameter `count` to vertex query.

* Renamed default _client_name to `hiro-graph-client` (name of the lib on PyPI).

# v4.7.0

* Content- / Media-Type handling

    * Throw `WrongContentTypeError` when an unexpected (or missing) Content-Type is returned.
    * Check for Content-Type of error results for constructing HTTPError.

* Error handling

    * Separate error message extraction, so it can be overwritten per API.
    * Try to handle different message formats by guessing, where their error message might be.
    * Intercept special error messages for API `ki`.
    * Flag `log_communication_on_error` to enable logging of communication when an error is detected in the response.

* Add parameter `max_tries` to TokenHandlers.

# v4.6.2

* Added documentation of new APIs

# v4.6.1

* Updated CHANGELOG.md

# v4.6.0

* Added the following APIS:
    * KI
    * AuthZ
    * Variables
* Adjust return value typing with API methods that return lists of dicts.

# v4.5.2

BUGFIX

* Previous versions incorrectly translated True and False to "True" and
  "False" whereas "true" and "false" (lowercase) are needed in URL queries. Fixed.

# v4.5.1

* Add `decode_token()` to decode the information embedded in a HIRO token.

# v4.5.0

* GitHub repository got renamed from `python-hiro-clients` to `hiro-client-python`. No other technical changes.

# v4.4.0

* Added IAM client
* Updated Graph client and Auth client
* put_binary is allowed to return simple strings now. (i.e. for avatar image updates).

# v4.3.0

* Adding SSL configuration

# v4.2.14

* Removed bug with reversed operator in websocketlib.
* Updated installation instructions in README.md.

# v4.2.13

* You need to explicitly set `query_params={'allscopes': 'true'}` if you want to enable it for EventWebSockets. If this
  is left out of the query_params, it will be added as 'allscopes': 'false'.

# v4.2.12

* Use typing to make sure, that `query_params=` for WebSockets is of type `Dict[str, str]`.
* Set `query_params={'allscopes': 'false'}` as default for the EventWebSocket.

# v4.2.11

* Debugged EventWebSocket handling.
* Abort connection when setting scope or filters failed.

# v4.2.10

* Adding scopes to EventWebSockets.

# v4.2.9

* Documentation of feature in v4.2.8

# v4.2.8

* WebSockets have new option `query_params` to add arbitrary query parameters to the initial websocket request.

# v4.2.7

Changes to `AbstractAuthenticatedWebSocketHandler`:

* Introducing `run_forever()` which will return after the reader thread has been joined. This can happen when another
  thread calls `stop()` (normal exit) or an internal error occurs, either directly when the connection is attempted, a
  token got invalid and could not be refreshed, or any other exception that has been thrown in the internal reader
  thread.

  This ensures, that the main thread does not continue when the websocket reader thread is not there.

* Enable parallel executions of `send()` across multiple threads.

* Make sure, that only one thread triggers a restart by a call to `restart()`.

* Check for active websocket reader thread via `is_active()`.

* Update examples for websockets in README.md.

Generic

* Update README.md to show usage of `client_name`.

# v4.2.6

* Do not require package uuid - it is already supplied with python

# v4.2.5

* Send valid close messages to backend.
* Introduced parameter `client_name` to give connections a name and also set header `User-Agent` more easily.

# v4.2.4

* Updated CHANGELOG.md.

# v4.2.3

* Hardening of clientlib. Removed some None-Value-Errors.

# v4.2.2

* Introduce parameter `remote_exit_codes` to `AbstractAuthenticatedWebSocketHandler`.

# v4.2.1

* Avoid blocking thread in `_backoff()` by not using `sleep()` but `threading.Condition.wait()`.

# v4.2.0

* Implement websocket protocols
    * event-ws
    * action-ws

# v4.1.3

* Use yield from instead of return

# v4.1.2

* Removed a bug with double yields on binary data

# v4.1.1

* Only log request/responses when logging.DEBUG is enabled

# v4.1.0

* Added timeseries handling to command `handle_vertices_combined`

# v4.0.0

* `AbstractTokenApiHandler`
    * Better token handling.
    * Resolve graph api endpoints via calling /api/version.
        * Ability to customize headers. Headers are handled case-insensitively and are submitted to requests
          capitalized.
        * Ability to override internal endpoints.


* AbstractIOCarrier works with `with` statements now.
* Added `BasicFileIOCarrier`.


* Removed `ApiConfig`.
* Renamed several internal classes.
* Better error messages.
* HTTP secure protocol logging.
* Fixed timestamp creation for tokens.


* Joe's suggestions - thanks Joe!

# v3.1.0

* Separation of APIs in client libraries. Currently, supported APIs are:
    * HiroGraph: https://core.arago.co/help/specs/?url=definitions/graph.yaml
    * HiroAuth: https://core.arago.co/help/specs/?url=definitions/auth.yaml
    * HiroApp: https://core.arago.co/help/specs/?url=definitions/app.yaml
* Use correct headers with binary transfers.
* Added gremlin and multi-id queries to HiroGraph.

# v3.0.0

* Renamed classes to match documentation elsewhere (i.e. Graphit -> HiroGraph, GraphitBatch -> HiroGraphBatch).
* Catch token expired error when refresh_token has expired.
* Documentation with examples

# v2.4.2

Added VERSION to package_data in setup.py

# v2.4.1

Added documentation for PyPI

# v2.4.0

Initial release after split from https://github.com/arago/hiro-clients

