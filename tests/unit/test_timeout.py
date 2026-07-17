from unittest.mock import MagicMock

import requests

from hiro_graph_client.clientlib import AbstractAPI


def _make_api(**kwargs) -> AbstractAPI:
    return AbstractAPI(root_url="https://example.invalid", session=requests.Session(), **kwargs)


class TestTimeout:
    """
    Tests for the connect/read timeout handling in :class:`AbstractAPI`. A single scalar timeout is combined with the
    default connect timeout while a *(connect, read)* tuple is passed straight to *requests*.
    """

    def test_default_timeout_is_connect_read_tuple(self):
        api = _make_api()
        assert api._timeout == (10, 600)

    def test_scalar_timeout_is_combined_with_default_connect(self):
        api = _make_api(timeout=45)
        assert api._timeout == (10, 45)

    def test_tuple_timeout_is_passed_through(self):
        api = _make_api(timeout=(2, 90))
        assert api._timeout == (2, 90)

    def test_list_timeout_is_normalized_to_tuple(self):
        api = _make_api(timeout=[2, 90])
        assert api._timeout == (2, 90)

    def test_none_falls_back_to_default(self):
        api = _make_api(timeout=None)
        assert api._timeout == (10, 600)

    def test_normalize_timeout_helper(self):
        api = _make_api()
        assert api._normalize_timeout(None) == (10, 600)
        assert api._normalize_timeout(30) == (10, 30)
        assert api._normalize_timeout((3, 120)) == (3, 120)
        assert api._normalize_timeout([3, 120]) == (3, 120)

    def test_timeout_tuple_is_forwarded_to_requests(self):
        """The normalized *(connect, read)* tuple must reach the underlying *requests* call unchanged."""

        class _Stop(Exception):
            """Non-RequestException so backoff does not retry and *_parse_response* is never reached."""

        captured = {}

        def _capture(*_args, **kwargs):
            captured['timeout'] = kwargs.get('timeout')
            raise _Stop()

        session = MagicMock(spec=requests.Session)
        session.get.side_effect = _capture

        api = AbstractAPI(root_url="https://example.invalid", session=session, timeout=(2, 90), max_tries=1)

        try:
            api.get("https://example.invalid/foo")
        except _Stop:
            pass

        assert captured['timeout'] == (2, 90)
