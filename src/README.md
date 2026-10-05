# HIRO Graph API Client

This is a client library to access data of the [HIRO Graph](#graph-client-hirograph).

This library also contains classes for handling the [WebSockets](#websockets) `event-ws` and `action-ws` API.

To process large batches of data, take a look at "hiro-batch-client". - (
PyPI: [hiro-batch-client](https://pypi.org/project/hiro-batch-client/),
GitHub: [hiro-batch-client-python](https://github.com/arago/hiro-batch-client-python))

For more information about HIRO Automation, look at https://www.almato.com/de/loesungen/hiro-ai.

For more information about the APIs this library covers, see https://dev-portal.engine.datagroup.de/7.0/.

Currently, implemented are

* `HiroApp` for `app`
* `HiroAuth` for `auth`
* `HiroGraph` for `graph`
* `HiroIam` for `iam`
* `HiroKi` for `ki`
* `HiroAuthz` for `authz`
* `HiroVariables` for `variables`

and for the websockets

* `AbstractEventWebSocketHandler` for `event-ws`
* `AbstractActionWebSocketHandler` for `action-ws`

## Quickstart

To use this library, you will need an account at https://desktop.engine.datagroup.de/ and access to an OAuth Client-Id and Client-Secret
to access the HIRO Graph. See also https://dev-portal.engine.datagroup.de/7.0/.

Most of the documentation is done in the sourcecode.

### HiroGraph Example

Example to use the straightforward graph api client:

```python
from hiro_graph_client import PasswordAuthTokenApiHandler, HiroGraph

hiro_client: HiroGraph = HiroGraph(
    api_handler=PasswordAuthTokenApiHandler(
        root_url="https://core.engine.datagroup.de",
        username='',
        password='',
        client_id='',
        client_secret=''
    )
)

# The commands of the Graph API are methods of the class HIROGraph.
# The next line executes a vertex query for instance. 
query_result = hiro_client.query('ogit\\/_type:"ogit/MARS/Machine"')

print(query_result)
```

## TokenApiHandler

Authorization against the HIRO Graph is done via tokens. These tokens are handled by classes of
type `AbstractTokenApiHandler` in this library. Each of the Hiro-Client-Object (`HiroGraph`, , `HiroApp`, etc.) need to
have some kind of TokenApiHandler at construction.

This TokenApiHandler is also responsible to determine the most up-to-date endpoints for the API calls. You can supply a
custom list of endpoints by using the dict parameter `custom_endpoints=` on construction.

A custom list of headers can also be set via the dict parameter `headers=` in the constructor. These would update the
internal headers. Header names can be supplied in any upper/lower-case.

This library supplies the following TokenApiHandlers:

---

### FixedTokenApiHandler

A simple TokenApiHandler that is generated with a preset-token at construction. Cannot update its token.

---

### EnvironmentTokenApiHandler

A TokenApiHandler that reads an environment variable (default is `HIRO_TOKEN`) from the runtime environment. Will only
update its token when the environment variable changes externally.

---

### PasswordAuthTokenApiHandler

This TokenApiHandler logs into the HiroAuth backend and obtains a token from login credentials. This is also the only
TokenApiHandler (so far) that automatically tries to renew a token from the backend when it has expired.

---

All code examples in this documentation can use these TokenApiHandlers interchangeably, depending on how such a token is
provided.

The HiroGraph example from above with another customized TokenApiHandler:

```python
from hiro_graph_client import EnvironmentTokenApiHandler, HiroGraph

hiro_client: HiroGraph = HiroGraph(
    api_handler=EnvironmentTokenApiHandler(
        root_url="https://core.engine.datagroup.de"
    )
)

# The commands of the Graph API are methods of the class HIROGraph.
# The next line executes a vertex query for instance. 
query_result = hiro_client.query('ogit\\/_type:"ogit/MARS/Machine"')

print(query_result)
```

Example with additional parameters:

```python
from hiro_graph_client import EnvironmentTokenApiHandler, HiroGraph

hiro_client: HiroGraph = HiroGraph(
    api_handler=EnvironmentTokenApiHandler(
        root_url="https://core.engine.datagroup.de",
        env_var='_TOKEN',
        headers={
            'X-Custom-Header': 'My custom value'
        },
        custom_endpoints={
            "graph": "/api/graph/7.2",
            "auth": "/api/auth/6.2"
        },
        client_name="HiroGraph (testing)"  # Will be used in the header 'User-Agent'
    )
)

# The commands of the Graph API are methods of the class HIROGraph.
# The next line executes a vertex query for instance. 
query_result = hiro_client.query('ogit\\/_type:"ogit/MARS/Machine"')

print(query_result)
```

## Token Handler sharing

When you need to access multiple APIs of HIRO, share the TokenApiHandler between the API objects to avoid unnecessary
requests for token- and version-information against HIRO. The TokenApiHandler will share a `requests.Session`, token-
and version-request-handling, and the token string itself between them.

```python
from hiro_graph_client import HiroGraph, HiroApp, PasswordAuthTokenApiHandler

hiro_api_handler = PasswordAuthTokenApiHandler(
    root_url="https://core.engine.datagroup.de",
    username='',
    password='',
    client_id='',
    client_secret=''
)

hiro_client: HiroGraph = HiroGraph(
    api_handler=hiro_api_handler
)

hiro_app_client: HiroApp = HiroApp(
    api_handler=hiro_api_handler
)
```

## Connection sharing

You can also let TokenApiHandlers share a common connection session instead of letting each of them create their own.
This might prove useful in a multithreading environment where tokens have to be set externally or change often (i.e.
one token per user per thread). This also ensures, that version-requests happen only once when the connection is
initialized.

Use the parameters `pool_maxsize` and `pool_block` to further tune the connection parameters for parallel access to 
the backend. See [requests Session Objects](https://docs.python-requests.org/en/latest/user/advanced/#session-objects)
and the Python documentation of `requests.adapters.HTTPAdapter` and `GraphConnectionHandler`.

```python
from hiro_graph_client import HiroGraph, HiroApp, FixedTokenApiHandler, GraphConnectionHandler

connection_handler = GraphConnectionHandler(
    root_url="https://core.engine.datagroup.de",
    pool_maxsize=200,                     # Optional: Max pool of cached connections for this connection session
    pool_block=True,                      # Optional: Do not allow more parallel connections than pool_maxsize
    client_name="Your Graph Client 0.0.1" # Optional: Will be used in the header 'User-Agent'
)

# Work with token of user 1

user1_client: HiroGraph = HiroGraph(
    api_handler=FixedTokenApiHandler(
        connection_handler=connection_handler,
        token='token user 1'
    )
)

# Work with token of user 2 (Shared Token Handler)

user2_api_handler = FixedTokenApiHandler(
    connection_handler=connection_handler,
    token='token user 2'
)

user2_client: HiroGraph = HiroGraph(
    api_handler=user2_api_handler
)

hiro_app_client: HiroApp = HiroApp(
    api_handler=user2_api_handler
)

```

Everything written in [Token Handler Sharing](#token-handler-sharing) still applies.

## SSL Configuration

SSL parameters are configured using the class `SSLConfig`. This class translates the parameters given to the required
fields for the `requests` library of Python (parameters `cert` and `verify` there). This configuration is given to the
TokenApiHandlers and will be used by the clients attached to it as well.

If this is not set, the default settings of the library `requests` will be used, which is to verify any server
certificates by using system defaults.

#### Example: Disable verification

```python
from hiro_graph_client import EnvironmentTokenApiHandler, HiroGraph, SSLConfig

hiro_client: HiroGraph = HiroGraph(
    api_handler=EnvironmentTokenApiHandler(
        root_url="https://core.engine.datagroup.de",
        # Disable any verification.
        ssl_config=SSLConfig(verify=False)
    )
)

query_result = hiro_client.query('ogit\\/_type:"ogit/MARS/Machine"')

print(query_result)
```

#### Example: Set custom SSL certificates

```python
from hiro_graph_client import EnvironmentTokenApiHandler, HiroGraph, SSLConfig

hiro_client: HiroGraph = HiroGraph(
    api_handler=EnvironmentTokenApiHandler(
        root_url="https://core.engine.datagroup.de",
        # Set custom certification files. If any of them are omitted, system defaults will be used.
        ssl_config=SSLConfig(
            cert_file="<path to client certificate file>",
            key_file="<path to key file for the client certificate>",
            ca_bundle_file="<path to the ca_bundle to verify the server certificate>"
        )
    )
)

query_result = hiro_client.query('ogit\\/_type:"ogit/MARS/Machine"')

print(query_result)
```

## Graph Client "HiroGraph"

The Graph Client is mostly straightforward to use, since all public methods of this class represent an API call in the
[Graph API](https://core.engine.datagroup.de/help/specs/?url=definitions/graph.yaml). Documentation is available in source code as
well. Some calls are a bit more complicated though and explained in more detail below:

### Attachments

To upload data to such a vertex, use `HiroGraph.post_attachment(data=...)`. The parameter `data=` will be given directly
to the call of the Python library `requests` as  `requests.post(data=...)`. To stream data, set `data` to an object of
type `IO`. See the documentation of the Python library `requests` for more details.

Downloading an attachment is done in chunks, so huge blobs of data in memory can be avoided when streaming this data.
Each chunk is 64k by default.

To stream such an attachment to a file, see the example below:

```python
ogit_id = '<ogit/_id of a vertex>'
data_iter = hiro_client.get_attachment(ogit_id)

with io.start("attachment.bin", "wb") as file:
    for chunk in data_iter:
        file.write(chunk)
```

To read the complete data in memory, see this example:

```python
ogit_id = '<ogit/_id of a vertex>'
data_iter = hiro_client.get_attachment(ogit_id)

attachment = b''.join(data_iter)
```

## Timeouts and error handling

### Setting timeouts

All clients and handlers accept a `timeout` (in seconds). It is either a tuple `(connect, read)` or a single scalar,
which is used as the read timeout and combined with the default connect timeout of 10 seconds. The default is
`(10, 600)`.

```python
from hiro_graph_client import PasswordAuthTokenApiHandler

# 5s to establish the connection, 60s to wait for the response
hiro_token_handler = PasswordAuthTokenApiHandler(
    root_url='https://core.engine.datagroup.de',
    username='', password='', client_id='', client_secret='',
    timeout=(5, 60)
)
```

Note that `timeout` applies **per attempt**, not to the call as a whole. Failing requests are retried with backoff
(see `max_tries`, default 2), so the worst case for an unreachable host is roughly
`max_tries * (connect + read) + backoff delays`. Set `max_tries=1` to disable retrying.

### Catching timeouts

A timeout raises `HiroTimeoutError`. Catch it to handle every kind of timeout:

```python
from hiro_graph_client import HiroTimeoutError

try:
    hiro_client.create_node(data, 'ogit/Person')
except HiroTimeoutError as err:
    print(f"Request timed out: {err}")
```

`HiroTimeoutError` inherits from both `requests.exceptions.Timeout` and the builtin `TimeoutError`, so
`except TimeoutError` and `except requests.exceptions.Timeout` catch it as well.

This matters because `requests` on its own raises a *different* exception class depending on when the timeout happens,
and one of them is not a `Timeout` at all:

| Timeout occurs while ...        | `requests` raises  | this library raises        |
|---------------------------------|--------------------|----------------------------|
| establishing the connection     | `ConnectTimeout`   | `HiroConnectTimeoutError`  |
| waiting for the response header | `ReadTimeout`      | `HiroReadTimeoutError`     |
| reading the response body       | `ConnectionError`  | `HiroBodyTimeoutError`     |

Because a timeout while reading the response body surfaces as a plain `ConnectionError`, a
`try ... except requests.exceptions.Timeout` block does **not** catch it and the script fails with an uncaught
exception. All three cases are subclasses of `HiroTimeoutError`, so catching that one class is enough.

The subclasses keep the original `requests` class as an additional base class, so existing error handling that
catches `requests.exceptions.ConnectTimeout`, `ReadTimeout` or `ConnectionError` keeps working unchanged.

### Rate limiting (status 429)

The HIRO rate limiter answers with status 429 and a `Retry-After` header (seconds) when a limit is exceeded. All
clients handle this transparently: the request is repeated after waiting for `Retry-After` (plus a small jitter, or
1s, 2s, 4s... when the header is missing). This is independent from the `max_tries` retries for 5xx errors and
connection problems.

```python
from hiro_graph_client import HiroGraph, PasswordAuthTokenApiHandler
from hiro_graph_client.clientlib import HiroRateLimitError

handler = PasswordAuthTokenApiHandler(root_url="https://core.engine.datagroup.de",
                                      username="...", password="...", client_id="...", client_secret="...",
                                      rate_limit_max_retries=3,  # Default. Use 0 to never retry.
                                      rate_limit_max_wait=60)  # Default. Max seconds for one wait. 0: never wait.

hiro_client = HiroGraph(api_handler=handler)

try:
    hiro_client.query('ogit/_type:"ogit/Automation/AutomationIssue"')
except HiroRateLimitError as err:
    # Raised when all retries are used up or the demanded wait exceeds rate_limit_max_wait.
    print(err.retry_after, err.policy, err.ratelimit)
```

`HiroRateLimitError` is a subclass of `requests.exceptions.HTTPError`. The options are set on the handler and inherited
by all clients that use it via `api_handler=`, like the other connection settings. Streaming downloads
(`get_binary`) are only repeated as long as no data has been received yet.

WebSockets that get a 429 while connecting do not fail. They wait at least `Retry-After` seconds and reconnect.

### Other errors

HTTP error responses raise `requests.exceptions.HTTPError` unless the handler has been created with
`raise_exceptions=False`. Problems with tokens raise `AuthenticationTokenError` or its subclasses
`TokenUnauthorizedError` and `FixedTokenError`. To catch anything a request can throw, use
`requests.exceptions.RequestException`, which is a base class of all of the above except the token errors.

## WebSockets

This library contains classes that make using HIRO WebSocket protocols easier. They handle authentication, exceptions
and much more.

The classes do not handle buffering of messages, so it is the duty of the programmer to ensure, that incoming messages
are either handled quickly or being buffered to avoid clogging the websocket. The classes are thread-safe, so it is
possible to handle each incoming message asynchronously in its own thread and have those threads send results back if
needed (See multithreaded example in [Action WebSocket](#action-websocket)).

### Closing WebSockets

To shut these WebSockets (_ws_) down cleanly, please consider these scenarios:

#### Default behaviour

The library reacts on *KeyboardInterrupt* (SIGINT) and closes the WebSocket cleanly with closing message to the sever
when such an interrupt is received. If another signal (like SIGTERM) is received, the program will stop immediately
without any closing message back to the server.

#### Signal handling

When installing signal handlers, you need to use *ws.signal_stop()* to shut the WebSocket down. Do NOT use
*ws.stop()* or the closing process will deadlock. This is because the signal interrupt is executed in the same thread
as *ws.run_forever()*.

Example:

```python
import signal

[...]

with ActionWebSocket(api_handler=FixedTokenApiHandler('HIRO_TOKEN')) as ws:
    def signal_handler(signum, handler):
        ws.signal_stop()


    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)  # This signal might not be available on MS Windows 

    ws.run_forever()
```

#### Closing from a separate thread

When closing the WebSocket from another thread than the one running *ws.run_forever()*, you should use *ws.stop()*. This
ensures, that the shutdown is synchronized and *ws.stop()* will return after the WebSocket has been closed.

### Event WebSocket

This websocket receives notifications about changes to vertices that match a certain filter.

See also [API description of event-ws](https://core.engine.datagroup.de/help/specs/?url=definitions/events-ws.yaml)

Example:

```python
import threading

from hiro_graph_client.clientlib import FixedTokenApiHandler
from hiro_graph_client.eventswebsocket import AbstractEventsWebSocketHandler, EventMessage, EventsFilter


class EventsWebSocket(AbstractEventsWebSocketHandler):

    def on_create(self, message: EventMessage):
        """ Vertex has been created """
        print("Create:\n" + str(message))

    def on_update(self, message: EventMessage):
        """ Vertex has been updated """
        print("Update:\n" + str(message))

    def on_delete(self, message: EventMessage):
        """ Vertex has been removed """
        print("Delete:\n" + str(message))


events_filter = EventsFilter(filter_id='testfilter', filter_content="(element.ogit/_type=ogit/MARS/Machine)")

with EventsWebSocket(api_handler=FixedTokenApiHandler('HIRO_TOKEN'),
                     events_filters=[events_filter],
                     query_params={"allscopes": "false", "delta": "false"}) as ws:
    ws.run_forever()  # Use KeyboardInterrupt (Ctrl-C) to exit. 

```

If you do not set the parameter `scope=`, the default scope of your account will be used. If you need to set the scope
by hand, use the following:

```python
[...]

api_handler = FixedTokenApiHandler('HIRO_TOKEN')

default_scope = api_handler.decode_token()['data']['default-scope']

with EventsWebSocket(api_handler=api_handler,
                     events_filters=[events_filter],
                     scopes=[default_scope],
                     query_params={"allscopes": "false", "delta": "false"}) as ws:
    ws.run_forever()  # Use KeyboardInterrupt (Ctrl-C) to exit. 

```

### Action WebSocket

This websocket receives notifications about actions that have been triggered within a KI. Use this to write your own
custom action handler.

See also [API description of action-ws](https://core.engine.datagroup.de/help/specs/?url=definitions/action-ws.yaml)

Simple example:

```python
import threading

from hiro_graph_client.actionwebsocket import AbstractActionWebSocketHandler
from hiro_graph_client.clientlib import FixedTokenApiHandler


class ActionWebSocket(AbstractActionWebSocketHandler):

    def on_submit_action(self, action_id: str, capability: str, parameters: dict):
        """ Message *submitAction* has been received """

        # Handle the message
        print(f"ID: {action_id}, Capability: {capability}, Parameters: {str(parameters)}")

        # Send back message *sendActionResult*
        self.send_action_result(action_id, "Everything went fine.")

    def on_config_changed(self):
        """ The configuration of the ActionHandler has changed """
        pass


with ActionWebSocket(api_handler=FixedTokenApiHandler('HIRO_TOKEN')) as ws:
    ws.run_forever()  # Use KeyboardInterrupt (Ctrl-C) to exit. 

```

Multithreading example using a thread executor:

```python
import threading
import concurrent.futures

from hiro_graph_client.actionwebsocket import AbstractActionWebSocketHandler
from hiro_graph_client.clientlib import FixedTokenApiHandler, AbstractTokenApiHandler


class ActionWebSocket(AbstractActionWebSocketHandler):

    def __init__(self, api_handler: AbstractTokenApiHandler):
        """ Initialize properties """
        super().__init__(api_handler)
        self._executor = None

    def start(self) -> None:
        """ Initialize the executor """
        super().start()
        self._executor = concurrent.futures.ThreadPoolExecutor()

    def stop(self, timeout: int = None) -> None:
        """ Shut the executor down """
        if self._executor:
            self._executor.shutdown()
        self._executor = None
        super().stop(timeout)

    def handle_submit_action(self, action_id: str, capability: str, parameters: dict):
        """ Runs asynchronously in its own thread. """
        print(f"ID: {action_id}, Capability: {capability}, Parameters: {str(parameters)}")
        self.send_action_result(action_id, "Everything went fine.")

    def on_submit_action(self, action_id: str, capability: str, parameters: dict):
        """ Message *submitAction* has been received. Message is handled in thread executor. """
        if not self._executor:
            raise RuntimeError('ActionWebSocket has not been started.')
        self._executor.submit(ActionWebSocket.handle_submit_action, self, action_id, capability, parameters)

    def on_config_changed(self):
        """ The configuration of the ActionHandler has changed """
        pass


with ActionWebSocket(api_handler=FixedTokenApiHandler('HIRO_TOKEN')) as ws:
    ws.run_forever()  # Use KeyboardInterrupt (Ctrl-C) to exit. 

```
