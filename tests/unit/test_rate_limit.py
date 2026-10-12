from unittest.mock import MagicMock, patch

import pytest
import requests
from websocket import WebSocketBadStatusException

from hiro_graph_client.clientlib import AbstractAPI, HiroRateLimitError, parse_retry_after

URL = "https://example.invalid/foo"
SLEEP = "hiro_graph_client.clientlib.time.sleep"


def _response(status: int, body: str = '{"ok": true}', headers: dict = None) -> requests.Response:
    res = requests.Response()
    res.status_code = status
    res.url = URL
    res._content = body.encode()
    res.headers.update({"Content-Type": "application/json", **(headers or {})})
    res.request = requests.Request("GET", URL).prepare()
    return res


def _limited(retry_after: str = "2") -> requests.Response:
    headers = {
        "ratelimit-policy": '"authed";q=5;w=60',
        "ratelimit": '"authed";r=0;t=2'
    }
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return _response(429,
                     '{"error": {"message": "Rate limit (authed) exceeded. Retry after 2000ms.", '
                     '"code": 429, "from": "rate-limiter"}}',
                     headers)


def _make_api(*responses, **kwargs) -> (AbstractAPI, MagicMock):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    return AbstractAPI(root_url="https://example.invalid", session=session, **kwargs), session


class TestParseRetryAfter:
    def test_seconds(self):
        assert parse_retry_after("12") == 12.0

    def test_fractional_seconds(self):
        assert parse_retry_after("1.5") == 1.5

    def test_http_date(self):
        with patch("hiro_graph_client.clientlib.time.time", return_value=1445412480):
            assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT") == 10.0

    def test_http_date_in_the_past_is_zero(self):
        with patch("hiro_graph_client.clientlib.time.time", return_value=1445412500):
            assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    @pytest.mark.parametrize("value", [None, "", "soon", "-3"])
    def test_invalid_is_none(self, value):
        assert parse_retry_after(value) is None


class TestRateLimitError:
    def test_is_http_error_with_details(self):
        api, _ = _make_api(_limited("7"), max_tries=1, rate_limit_max_retries=0)

        with pytest.raises(HiroRateLimitError) as info:
            api.get(URL)

        err = info.value
        assert isinstance(err, requests.exceptions.HTTPError)
        assert err.response.status_code == 429
        assert err.retry_after == 7.0
        assert err.policy == '"authed";q=5;w=60'
        assert err.ratelimit == '"authed";r=0;t=2'
        assert "Rate limit (authed) exceeded" in str(err)

    def test_missing_retry_after_is_none(self):
        api, _ = _make_api(_limited(None), rate_limit_max_retries=0)

        with pytest.raises(HiroRateLimitError) as info:
            api.get(URL)

        assert info.value.retry_after is None

    def test_other_4xx_is_plain_http_error(self):
        api, _ = _make_api(_response(404, '{"error": {"message": "nope"}}'))

        with pytest.raises(requests.exceptions.HTTPError) as info:
            api.get(URL)

        assert not isinstance(info.value, HiroRateLimitError)


class TestRateLimitRetry:
    def test_defaults(self):
        api, _ = _make_api()
        assert api._rate_limit_max_retries == 3
        assert api._rate_limit_max_wait == 60

    def test_options_are_copied_from_abstract_api(self):
        source, _ = _make_api(rate_limit_max_retries=7, rate_limit_max_wait=0)
        copy = AbstractAPI(abstract_api=source)
        assert copy._rate_limit_max_retries == 7
        assert copy._rate_limit_max_wait == 0

    def test_waits_retry_after_then_succeeds(self):
        api, session = _make_api(_limited("2"), _response(200))

        with patch(SLEEP) as sleep:
            assert api.get(URL) == {"ok": True}

        assert session.get.call_count == 2
        assert sleep.call_count == 1
        assert 2.0 <= sleep.call_args[0][0] <= 2.2

    def test_wait_with_jitter_never_exceeds_cap(self):
        api, _ = _make_api(_limited("60"), _response(200), rate_limit_max_wait=60)

        with patch(SLEEP) as sleep, patch("hiro_graph_client.clientlib.random.uniform", side_effect=lambda a, b: b):
            api.get(URL)

        assert sleep.call_args[0][0] == 60

    def test_gives_up_after_max_retries(self):
        api, session = _make_api(*[_limited("1")] * 4, rate_limit_max_retries=3)

        with patch(SLEEP) as sleep, pytest.raises(HiroRateLimitError) as info:
            api.get(URL)

        assert session.get.call_count == 4
        assert sleep.call_count == 3
        assert info.value.retry_after == 1.0

    def test_wait_above_cap_raises_without_sleeping(self):
        api, session = _make_api(_limited("120"), rate_limit_max_wait=60)

        with patch(SLEEP) as sleep, pytest.raises(HiroRateLimitError):
            api.get(URL)

        assert session.get.call_count == 1
        sleep.assert_not_called()

    def test_zero_max_wait_disables_waiting(self):
        api, session = _make_api(_limited("1"), rate_limit_max_wait=0)

        with patch(SLEEP) as sleep, pytest.raises(HiroRateLimitError):
            api.get(URL)

        assert session.get.call_count == 1
        sleep.assert_not_called()

    def test_missing_retry_after_uses_exponential_wait(self):
        api, _ = _make_api(_limited(None), _limited(None), _response(200))

        with patch(SLEEP) as sleep:
            assert api.get(URL) == {"ok": True}

        waits = [c[0][0] for c in sleep.call_args_list]
        assert 1.0 <= waits[0] <= 1.2
        assert 2.0 <= waits[1] <= 2.3

    def test_server_error_backoff_is_unchanged(self):
        """5xx is still handled by *backoff* with *max_tries* and never by the rate limit logic."""
        api, session = _make_api(_response(503, '{"error": "down"}'), _response(503, '{"error": "down"}'))

        with patch(SLEEP) as sleep, patch("backoff._sync.time.sleep"), \
                pytest.raises(requests.exceptions.HTTPError) as info:
            api.get(URL)

        assert not isinstance(info.value, HiroRateLimitError)
        assert session.get.call_count == 2
        sleep.assert_not_called()

    def test_post_is_retried_too(self):
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = [_limited("1"), _response(200)]
        api = AbstractAPI(root_url="https://example.invalid", session=session)

        with patch(SLEEP):
            assert api.post(URL, {"a": 1}) == {"ok": True}

        assert session.post.call_count == 2

    def test_get_binary_is_retried_before_first_chunk(self):
        ok = _response(200, "abc")
        ok.raw = MagicMock()
        ok.iter_content = lambda chunk_size: iter([b"abc"])
        limited = _limited("1")

        session = MagicMock(spec=requests.Session)
        session.get.return_value.__enter__.side_effect = [limited, ok]
        api = AbstractAPI(root_url="https://example.invalid", session=session)

        with patch(SLEEP) as sleep:
            assert b"".join(api.get_binary(URL)) == b"abc"

        assert sleep.call_count == 1


class TestWebSocketRateLimit:
    def _handler(self):
        from hiro_graph_client.websocketlib import AbstractAuthenticatedWebSocketHandler

        class _Handler(AbstractAuthenticatedWebSocketHandler):
            def on_message(self, ws, message): ...

        api_handler = MagicMock()
        api_handler.get_websocket_config.return_value = ("wss://example.invalid/ws", None, None, None, None)
        return _Handler(api_handler=api_handler, api_name="events-ws")

    def test_429_on_upgrade_is_not_fatal_and_sets_minimum_delay(self):
        from hiro_graph_client.websocketlib import ReaderStatus

        handler = self._handler()
        handler._reader_status = ReaderStatus.STARTING
        error = WebSocketBadStatusException("Handshake status %d %s", 429, None, {"retry-after": "30"})

        handler._check_error(None, error)

        assert handler._reader_status != ReaderStatus.FAILED
        assert handler._rate_limit_delay == 30.0

    def test_reconnect_backoff_honors_retry_after_with_jitter(self):
        handler = self._handler()
        handler._rate_limit_delay = 30.0

        with patch.object(handler._backoff_condition, "wait") as wait, \
                patch("hiro_graph_client.clientlib.random.uniform", side_effect=lambda a, b: b):
            handler._reconnect_backoff(1)

        assert 30.0 < wait.call_args[1]["timeout"] <= 33.0
        assert handler._rate_limit_delay is None

    def test_reconnect_backoff_without_retry_after_is_plain(self):
        handler = self._handler()

        with patch.object(handler._backoff_condition, "wait") as wait:
            assert handler._reconnect_backoff(3) == 4

        assert wait.call_args[1]["timeout"] == 3

    def test_send_backoff_does_not_consume_retry_after(self):
        """*send()* runs in worker threads. It must neither wait for nor clear the delay meant for the reconnect."""
        handler = self._handler()
        handler._rate_limit_delay = 30.0

        with patch.object(handler._backoff_condition, "wait") as wait:
            handler._backoff(0)

        wait.assert_not_called()
        assert handler._rate_limit_delay == 30.0

    def test_other_handshake_errors_still_fail(self):
        from hiro_graph_client.websocketlib import ReaderStatus

        handler = self._handler()
        handler._reader_status = ReaderStatus.STARTING

        handler._check_error(None, WebSocketBadStatusException("Handshake status %d %s", 403, None, {}))

        assert handler._reader_status == ReaderStatus.FAILED
