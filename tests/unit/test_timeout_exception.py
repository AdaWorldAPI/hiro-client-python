import socket
import threading

import pytest
import requests
import urllib3

from hiro_graph_client import HiroGraph, FixedTokenApiHandler, HiroTimeoutError, HiroConnectTimeoutError, \
    HiroReadTimeoutError, HiroBodyTimeoutError
from hiro_graph_client.clientlib import _is_timeout_error

VERSION_INFO = {"graph": {"endpoint": "/api/graph/7.2", "version": "7.2"}}


def _serve(responder) -> int:
    """
    Start a throwaway HTTP server on a random port that answers with *responder*, and return that port. *responder*
    gets the accepted socket and may stall to provoke a timeout. The server dies with the test process.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def loop():
        connections = []
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            connections.append(conn)  # keep referenced so it is not closed by the GC
            try:
                conn.recv(65536)
                responder(conn)
            except OSError:
                pass

    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


def _graph(port: int, timeout=(5, 1)) -> HiroGraph:
    handler = FixedTokenApiHandler(token="dummy", root_url=f"http://127.0.0.1:{port}",
                                   version_info=VERSION_INFO, timeout=timeout, max_tries=1)
    return HiroGraph(api_handler=handler)


def _stall(_conn):
    """Accept the connection but never answer -> timeout while waiting for the response headers."""


def _headers_then_stall(conn):
    """Announce a body, send only part of it, then stall -> timeout while reading the response body."""
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n")
    conn.sendall(b'{"partial"')


class TestTimeoutException:
    """
    Tests that every timeout leaves the client as a :class:`HiroTimeoutError`, no matter at which point of the request
    it happens. *requests* itself uses three different exception classes for this, which made reliable error handling
    in calling scripts practically impossible.
    """

    def test_is_a_requests_timeout(self):
        """Existing error handling that catches the *requests* exception must keep working."""
        assert issubclass(HiroTimeoutError, requests.exceptions.Timeout)

    def test_is_a_builtin_timeout_error(self):
        """`except TimeoutError` must catch it, which a plain requests.exceptions.Timeout does not."""
        assert issubclass(HiroTimeoutError, TimeoutError)

    def test_timeout_waiting_for_headers(self):
        """Times out before any response arrives - requests would raise ReadTimeout."""
        graph = _graph(_serve(_stall))
        with pytest.raises(HiroTimeoutError):
            graph.create_node({"ogit/name": "x"}, "ogit/Person")

    def test_timeout_while_reading_body(self):
        """
        Times out after the headers arrived. This is the case requests reports as a plain ConnectionError, so
        `except requests.exceptions.Timeout` used to miss it entirely.
        """
        graph = _graph(_serve(_headers_then_stall))
        with pytest.raises(HiroTimeoutError):
            graph.create_node({"ogit/name": "x"}, "ogit/Person")

    def test_timeout_while_reading_body_is_caught_as_builtin_timeout_error(self):
        graph = _graph(_serve(_headers_then_stall))
        with pytest.raises(TimeoutError):
            graph.create_node({"ogit/name": "x"}, "ogit/Person")

    def test_connect_timeout(self):
        """192.0.2.1 (TEST-NET-1) is routed but blackholed, so the connect attempt runs into its timeout."""
        handler = FixedTokenApiHandler(token="dummy", root_url="https://192.0.2.1",
                                       version_info=VERSION_INFO, timeout=(1, 5), max_tries=1)
        with pytest.raises(HiroTimeoutError):
            HiroGraph(api_handler=handler).create_node({"ogit/name": "x"}, "ogit/Person")

    def test_original_exception_is_kept_as_cause(self):
        """The original *requests* exception must stay available for debugging."""
        graph = _graph(_serve(_stall))
        with pytest.raises(HiroTimeoutError) as exc_info:
            graph.create_node({"ogit/name": "x"}, "ogit/Person")
        assert isinstance(exc_info.value.__cause__, requests.exceptions.RequestException)

    @pytest.mark.parametrize("hiro_class, requests_class", [
        (HiroConnectTimeoutError, requests.exceptions.ConnectTimeout),
        (HiroReadTimeoutError, requests.exceptions.ReadTimeout),
        (HiroBodyTimeoutError, requests.exceptions.ConnectionError),
    ])
    def test_original_requests_class_is_preserved(self, hiro_class, requests_class):
        """Existing error handling that catches a specific *requests* exception must keep working."""
        assert issubclass(hiro_class, requests_class)
        assert issubclass(hiro_class, HiroTimeoutError)
        assert issubclass(hiro_class, TimeoutError)

    def test_header_timeout_stays_a_read_timeout(self):
        graph = _graph(_serve(_stall))
        with pytest.raises(requests.exceptions.ReadTimeout):
            graph.create_node({"ogit/name": "x"}, "ogit/Person")

    def test_body_timeout_stays_a_connection_error(self):
        """This used to be the only way to catch it, so it must keep working."""
        graph = _graph(_serve(_headers_then_stall))
        with pytest.raises(requests.exceptions.ConnectionError):
            graph.create_node({"ogit/name": "x"}, "ogit/Person")

    def test_non_timeout_connection_error_is_not_translated(self):
        """A connection error that is not a timeout must keep its own class."""
        handler = FixedTokenApiHandler(token="dummy", root_url="http://127.0.0.1:1",
                                       version_info=VERSION_INFO, max_tries=1)

        def _refused(*_args, **_kwargs):
            raise requests.exceptions.ConnectionError("connection refused")

        handler._session.post = _refused

        with pytest.raises(requests.exceptions.ConnectionError) as exc_info:
            HiroGraph(api_handler=handler).create_node({"ogit/name": "x"}, "ogit/Person")
        assert not isinstance(exc_info.value, HiroTimeoutError)


class TestIsTimeoutError:
    """Tests for the cause-chain inspection that detects timeouts hidden inside a *ConnectionError*."""

    def test_detects_urllib3_read_timeout_in_args(self):
        err = requests.exceptions.ConnectionError(urllib3.exceptions.ReadTimeoutError(None, "/", "Read timed out."))
        assert _is_timeout_error(err) is True

    def test_detects_builtin_timeout_in_context(self):
        try:
            try:
                raise TimeoutError("timed out")
            except TimeoutError:
                raise requests.exceptions.ConnectionError("wrapped")
        except requests.exceptions.ConnectionError as err:
            assert _is_timeout_error(err) is True

    def test_plain_connection_error_is_not_a_timeout(self):
        assert _is_timeout_error(requests.exceptions.ConnectionError("connection refused")) is False

    def test_does_not_loop_on_self_referencing_cause(self):
        err = requests.exceptions.ConnectionError("boom")
        err.__cause__ = err
        assert _is_timeout_error(err) is False
