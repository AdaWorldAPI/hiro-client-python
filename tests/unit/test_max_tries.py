from unittest.mock import MagicMock

import pytest
import requests

from hiro_graph_client.clientlib import AbstractAPI


def _make_api(**kwargs) -> AbstractAPI:
    return AbstractAPI(root_url="https://example.invalid", session=requests.Session(), **kwargs)


class TestMaxTries:
    """
    Tests for the BACKOFF retry bound of :class:`AbstractAPI`. *backoff* treats ``max_tries=None`` as "retry forever",
    so an unset *max_tries* must fall back to the class default instead of being stored verbatim. Otherwise a request
    to an unreachable host keeps retrying and never honours its timeout.
    """

    def test_default_max_tries_is_applied(self):
        api = _make_api()
        assert api._max_tries == 2

    def test_get_max_tries_is_never_none(self):
        """*backoff* interprets *None* as an unlimited number of tries, so this must always be a finite number."""
        api = _make_api()
        assert api._get_max_tries() is not None

    def test_explicit_max_tries_is_kept(self):
        assert _make_api(max_tries=5)._max_tries == 5

    def test_max_tries_is_copied_from_abstract_api(self):
        source = _make_api(max_tries=4)
        assert AbstractAPI(abstract_api=source)._max_tries == 4

    def test_connection_error_gives_up_after_max_tries(self):
        """The reported symptom: with an unreachable host the request must stop retrying instead of hanging."""
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.exceptions.ConnectTimeout("host unreachable")

        api = AbstractAPI(root_url="https://example.invalid", session=session, timeout=10)

        with pytest.raises(requests.exceptions.ConnectTimeout):
            api.get("https://example.invalid/foo")

        assert session.get.call_count == 2
