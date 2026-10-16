import json
import time
from unittest.mock import MagicMock, patch

from hiro_graph_client.actionwebsocket import AbstractActionWebSocketHandler, ActionHandlerMessageParser, \
    ActionHandlerResult, ActionHandlerRateLimited, UnknownActionException

import pytest


class _Handler(AbstractActionWebSocketHandler):
    def on_submit_action(self, action_id, capability, parameters): ...

    def on_config_changed(self): ...


def _handler() -> AbstractActionWebSocketHandler:
    api_handler = MagicMock()
    api_handler.get_websocket_config.return_value = ("wss://example.invalid/ws", None, None, None, None)
    handler = _Handler(api_handler)
    handler.send = MagicMock()
    return handler


def _rate_limited(action_id="a1", retry_after_ms=1500) -> str:
    """The in-band answer of ws-proxy to a denied frame. Has no *type*."""
    return json.dumps({"error": {"code": 429, "retryAfterMs": retry_after_ms}, "id": action_id})


def _store_result(handler, action_id="a1") -> ActionHandlerResult:
    result = ActionHandlerResult(action_id, {"code": 200, "message": "ok"})
    handler.resultStore.add(int(time.time() * 1000) + 600_000, result)
    return result


class TestParser:
    def test_rate_limited_message_is_parsed(self):
        message = ActionHandlerMessageParser(_rate_limited("a1", 2500)).parse()

        assert isinstance(message, ActionHandlerRateLimited)
        assert message.id == "a1"
        assert message.retry_after_ms == 2500

    def test_missing_retry_after_is_none(self):
        message = ActionHandlerMessageParser(json.dumps({"error": {"code": 429}, "id": "a1"})).parse()

        assert message.retry_after_ms is None

    def test_429_without_id_is_parsed_but_has_no_id(self):
        message = ActionHandlerMessageParser(json.dumps({"error": {"code": 429, "retryAfterMs": 10}})).parse()

        assert isinstance(message, ActionHandlerRateLimited)
        assert message.id is None

    def test_other_untyped_messages_are_still_unknown(self):
        with pytest.raises(UnknownActionException):
            ActionHandlerMessageParser(json.dumps({"error": {"code": 500}, "id": "a1"})).parse()


class TestOnMessage:
    def test_stored_result_is_resent_after_retry_after_and_never_nacked(self):
        handler = _handler()
        result = _store_result(handler)
        timers = []

        with patch("hiro_graph_client.actionwebsocket.threading.Timer",
                   side_effect=lambda delay, fn: timers.append((delay, fn)) or MagicMock()):
            handler.on_message(None, _rate_limited("a1", 1500))

        handler.send.assert_not_called()
        assert len(timers) == 1
        assert 1.5 <= timers[0][0] <= 1.7

        timers[0][1]()

        handler.send.assert_called_once_with(result.stringify_result())

    def test_result_acknowledged_in_the_meantime_is_not_resent(self):
        handler = _handler()
        _store_result(handler)
        timers = []

        with patch("hiro_graph_client.actionwebsocket.threading.Timer",
                   side_effect=lambda delay, fn: timers.append(fn) or MagicMock()):
            handler.on_message(None, _rate_limited("a1"))

        handler.resultStore.remove("a1")
        timers[0]()

        handler.send.assert_not_called()

    def test_result_is_not_discarded_by_repeated_429(self):
        """Unlike a NACK, a rate limit must not use up the retries of the stored result."""
        handler = _handler()
        _store_result(handler)

        with patch("hiro_graph_client.actionwebsocket.threading.Timer"):
            for _ in range(10):
                handler.on_message(None, _rate_limited("a1"))

        assert handler.resultStore.get("a1") is not None

    def test_unknown_id_is_only_logged(self):
        handler = _handler()

        with patch("hiro_graph_client.actionwebsocket.threading.Timer") as timer:
            handler.on_message(None, _rate_limited("nope"))

        handler.send.assert_not_called()
        timer.assert_not_called()

    def test_429_without_id_is_only_logged(self):
        handler = _handler()

        handler.on_message(None, json.dumps({"error": {"code": 429, "retryAfterMs": 10}}))

        handler.send.assert_not_called()

    def test_missing_retry_after_uses_a_default_delay(self):
        handler = _handler()
        _store_result(handler)

        with patch("hiro_graph_client.actionwebsocket.threading.Timer") as timer:
            handler.on_message(None, json.dumps({"error": {"code": 429}, "id": "a1"}))

        assert timer.call_args[0][0] >= 1
