#!/usr/bin/env python3
import base64
import json
import logging
import os
import random
import socket
import threading
import time
import urllib
from abc import abstractmethod
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from typing import Optional, Any, Iterator, List, Union, Tuple
from urllib.parse import quote, urlencode

import backoff
import requests
import requests.adapters
import urllib3

from hiro_graph_client.version import __version__

logger = logging.getLogger(__name__)
""" The logger for this module """

BACKOFF_ARGS = [
    backoff.expo,
    requests.exceptions.RequestException
]
BACKOFF_KWARGS = {
    'jitter': backoff.random_jitter,
    'giveup': lambda e: e.response is not None and e.response.status_code < 500
}

def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """
    Parse the value of a *Retry-After* header (RFC 9110), which is either a number of seconds or an HTTP date.

    :param value: The raw header value.
    :return: The number of seconds to wait (never negative), or None when *value* is missing or invalid.
    """
    if not value:
        return None

    value = value.strip()

    try:
        seconds = float(value)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass

    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def jittered_wait(wait: float, cap: Optional[float] = None) -> float:
    """
    Add a little random jitter to a wait so clients that have been told the same *Retry-After* do not all come back
    at the same moment.

    :param wait: Seconds to wait at least.
    :param cap: Optional upper bound of the result. Never reduces the result below *wait*.
    :return: *wait* plus up to 10% (max 1s) of jitter.
    """
    jittered = wait + random.uniform(0, min(1.0, wait * 0.1))
    return jittered if cap is None else max(wait, min(jittered, cap))


TIMEOUT_CAUSES = (urllib3.exceptions.TimeoutError, socket.timeout, TimeoutError)
"""Exception types that identify an underlying timeout, no matter which *requests* exception wraps them."""


def _is_timeout_error(err: BaseException) -> bool:
    """
    Check whether *err* has been caused by a timeout.

    This is needed because *requests* does not report every timeout as a *requests.exceptions.Timeout*: when the
    timeout happens while the response *body* is being read, it surfaces as a plain *requests.exceptions.ConnectionError*
    instead. The original timeout is still reachable through the exception arguments and the __cause__/__context__
    chain, so search those instead of matching on the error message.

    :param err: The exception to inspect.
    :return: True when a timeout is found anywhere in the chain of causes.
    """
    pending: List[BaseException] = [err]
    seen: set = set()

    while pending:
        current = pending.pop()

        if id(current) in seen:
            continue
        seen.add(id(current))

        if isinstance(current, TIMEOUT_CAUSES):
            return True

        pending.extend(item for item in (*current.args, current.__cause__, current.__context__)
                       if isinstance(item, BaseException))

    return False


def _as_hiro_timeout(err: requests.exceptions.RequestException) -> 'HiroTimeoutError':
    """
    Create the :class:`HiroTimeoutError` matching *err*.

    The returned exception keeps the original *requests* class as a second base class, so error handling that catches
    the specific *requests* exception keeps working unchanged.

    :param err: The original timeout exception raised by *requests*.
    :return: The matching :class:`HiroTimeoutError` subclass.
    """
    if isinstance(err, requests.exceptions.ConnectTimeout):
        error_class = HiroConnectTimeoutError
    elif isinstance(err, requests.exceptions.ReadTimeout):
        error_class = HiroReadTimeoutError
    elif isinstance(err, requests.exceptions.ConnectionError):
        error_class = HiroBodyTimeoutError
    else:
        error_class = HiroTimeoutError

    return error_class(*err.args, request=err.request, response=err.response)


@contextmanager
def _translate_timeout_errors() -> Iterator[None]:
    """
    Context manager that normalizes all timeouts of *requests* so they can be caught as a single
    :class:`HiroTimeoutError`.

    Without this, the exception class raised on a timeout depends on *when* the timeout happens: *ConnectTimeout* while
    connecting, *ReadTimeout* while waiting for the response headers and *ConnectionError* while reading the response
    body. Callers had to know all three to handle a timeout reliably.

    :raises HiroTimeoutError: When the wrapped code times out.
    """
    try:
        yield
    except HiroTimeoutError:
        raise
    except requests.exceptions.Timeout as err:
        raise _as_hiro_timeout(err) from err
    except requests.exceptions.ConnectionError as err:
        if not _is_timeout_error(err):
            raise
        raise _as_hiro_timeout(err) from err


###################################################################################################################
# SSL Configuration
###################################################################################################################

class SSLConfig:
    verify: bool
    cert_file: str
    key_file: str
    ca_bundle_file: str

    """
    This class contains the configuration for SSL connections, like the files to use.
    """

    def __init__(self,
                 verify: bool = True,
                 cert_file: str = None,
                 key_file: str = None,
                 ca_bundle_file: str = None):
        """
        Configuration for SSL connections.

        :param verify: Verify connections at all. If just set to True without other parameters, defaults will be used.
        :param cert_file: (optional) Client certificate file (.pem).
        :param key_file: (optional) Key for the certificate file.
        :param ca_bundle_file: (optional) The ca_bundle for server certificate verification.
        """
        self.verify = False if verify is False else True
        self.cert_file = cert_file
        self.key_file = key_file
        self.ca_bundle_file = ca_bundle_file

    def get_verify(self):
        """
        Get verify parameter as expected by requests library.

        :return: True, False or a path to a ca_bundle.
        """
        if self.verify and self.ca_bundle_file:
            return self.ca_bundle_file
        return self.verify

    def get_cert(self):
        """
        Get cert parameter as expected by requests library.

        :return: Tuple of cert_file, key_file or just cert_file - which can be None.
        """
        if self.cert_file and self.key_file:
            return self.cert_file, self.key_file
        return self.cert_file


###################################################################################################################
# Root classes for API
###################################################################################################################

class AbstractAPI:
    """
    This abstract root class contains the methods for HTTP requests used by all API classes. Also contains several
    tool methods for handling headers, url query parts and response error checking.
    """

    _root_url: str = None
    """Servername and context path of the root of the API"""

    _session: requests.Session = None
    """Reference to the session information"""

    ssl_config: SSLConfig
    """Security configuration and location of certificate files"""

    _client_name: str = "hiro-graph-client"
    """Used in header 'User-Agent'"""

    _max_tries: int = 2
    """Retries for backoff"""

    _rate_limit_max_retries: int = 3
    """Number of times a request is repeated after the backend answered with status 429 (Too Many Requests)."""

    _rate_limit_max_wait: Union[int, float] = 60
    """Max seconds to wait for a single retry after status 429. A longer wait raises *HiroRateLimitError* instead."""

    _connect_timeout: Union[int, float] = 10
    """Default connect timeout (in seconds) used when *timeout* is given as a single scalar value."""

    _timeout: Union[int, float, Tuple[Union[int, float], Union[int, float]]] = (10, 600)
    """
    Timeout for requests-methods as needed by package 'requests'. This is a tuple *(connect, read)* so the connection
    establishment is bounded by a short connect timeout while the (potentially slow) read is bounded separately. A
    single scalar value is also accepted and will be combined with the default connect timeout.
    """

    _raise_exceptions: bool = True
    """Raise an exception when the status-code of results indicates an error"""

    _proxies: dict = None
    """Proxy configuration as needed by package 'requests'."""

    _headers: dict = {}
    """Common headers for HTTP requests."""

    _log_communication_on_error: bool = False
    """Dump request and response into logging on errors"""

    def __init__(self,
                 root_url: str = None,
                 session: requests.Session = None,
                 raise_exceptions: bool = True,
                 proxies: dict = None,
                 headers: dict = None,
                 timeout: Optional[Union[int, float, Tuple[Union[int, float], Union[int, float]]]] = None,
                 client_name: str = None,
                 ssl_config: SSLConfig = None,
                 log_communication_on_error: bool = None,
                 max_tries: int = None,
                 rate_limit_max_retries: int = None,
                 rate_limit_max_wait: Union[int, float] = None,
                 abstract_api=None):

        """
        Constructor

        A note regarding headers: If you set a value in the dict to *None*, it will not show up in the HTTP-request
        headers. Use this to erase entries from existing default headers or headers copied from *apstract_api* (when
        given).

        :param root_url: Root uri of the HIRO API, like *https://core.engine.datagroup.de*.
        :param session: The requests.Session object for the connection pool. Required.
        :param raise_exceptions: Raise exceptions on HTTP status codes that denote an error. Default is True.
        :param proxies: Proxy configuration for *requests*. Default is None.
        :param headers: Optional custom HTTP headers. Will be merged with the internal default headers. Default is None.
        :param timeout: Optional timeout (in seconds) for requests. Can be a single scalar (applied as read timeout,
               combined with the default connect timeout) or a tuple *(connect, read)* passed straight to *requests*.
               A short connect timeout avoids hanging for the full read timeout when a host is unreachable. Default is
               *(10, 600)*, i.e. a 10 second connect timeout and a 600 second (10 min) read timeout.
        :param client_name: Optional name for the client. Will also be part of the "User-Agent" header unless *headers*
               is given with another value for "User-Agent". Default is "hiro-graph-client".
        :param ssl_config: Optional configuration for SSL connections. If this is omitted, the defaults of `requests`
               lib will be used.
        :param log_communication_on_error: Log socket communication when an error (status_code of HTTP Response) is
               detected. Default is not to do this.
        :param max_tries: Max tries for BACKOFF. Default is 2.
        :param rate_limit_max_retries: How often a request is repeated when the backend answers with status 429 (rate
               limited), after waiting as demanded by the *Retry-After* header. Default is 3. Set to 0 to disable.
        :param rate_limit_max_wait: Max seconds to wait for a single retry after status 429. When the backend demands a
               longer wait, *HiroRateLimitError* is raised right away. Default is 60. Set to 0 to never wait.
        :param abstract_api: Set all parameters by copying them from the instance given by this parameter. Overrides
               all other parameters except headers, which will be merged with existing ones.
        """

        if isinstance(abstract_api, AbstractAPI):
            root_url = abstract_api._root_url
            session = abstract_api._session
            raise_exceptions = abstract_api._raise_exceptions
            proxies = abstract_api._proxies
            initial_headers = abstract_api._headers.copy()
            timeout = abstract_api._timeout
            client_name = abstract_api._client_name
            ssl_config = abstract_api.ssl_config
            log_communication_on_error = abstract_api._log_communication_on_error
            max_tries = abstract_api._max_tries
            rate_limit_max_retries = abstract_api._rate_limit_max_retries
            rate_limit_max_wait = abstract_api._rate_limit_max_wait
        else:
            initial_headers = {
                'Content-Type': 'application/json',
                'Accept': 'text/plain, application/json',
                'User-Agent': f"{client_name or self._client_name} {__version__}"
            }

        self._root_url = root_url
        self._session = session

        if not self._root_url:
            raise ValueError("'root_url' must not be empty.")

        if not self._session:
            raise ValueError("'session' must not be empty.")

        self._client_name = client_name or self._client_name
        self._headers = AbstractAPI._merge_headers(initial_headers, headers)

        self.ssl_config = ssl_config or SSLConfig()

        self._proxies = proxies
        self._raise_exceptions = raise_exceptions
        self._timeout = self._normalize_timeout(timeout)
        self._log_communication_on_error = log_communication_on_error or False
        self._max_tries = max_tries or self._max_tries
        if rate_limit_max_retries is not None:
            self._rate_limit_max_retries = rate_limit_max_retries
        if rate_limit_max_wait is not None:
            self._rate_limit_max_wait = rate_limit_max_wait

    def _normalize_timeout(
            self,
            timeout: Optional[Union[int, float, Tuple[Union[int, float], Union[int, float]]]]
    ) -> Union[int, float, Tuple[Union[int, float], Union[int, float]]]:
        """
        Normalize the *timeout* value into a form accepted by *requests*.

        A tuple *(connect, read)* is passed through unchanged. A single scalar is combined with the default connect
        timeout into a *(connect, read)* tuple so connection establishment does not hang for the full read timeout.
        *None* falls back to the current (already normalized) default.

        :param timeout: The timeout value given to the constructor.
        :return: Either a scalar or a *(connect, read)* tuple as understood by *requests*.
        """
        if timeout is None:
            return self._timeout
        if isinstance(timeout, (tuple, list)):
            return timeout[0], timeout[1]
        return self._connect_timeout, timeout

    def _get_max_tries(self):
        return self._max_tries

    def get_root_url(self):
        return self._root_url

    @property
    def user_agent(self):
        return self._headers.get('User-Agent') or self._client_name

    @staticmethod
    def _capitalize_header(name: str) -> str:
        return "-".join([n.capitalize() for n in name.split('-')])

    ###############################################################################################################
    # Basic requests
    ###############################################################################################################

    def get_binary(self, url: str, accept: str = None) -> Iterator[bytes]:
        """
        Implementation of GET for binary data.

        :param url: Url to use
        :param accept: Mimetype for accept. Will be set to */* if not given.
        :return: Yields over raw chunks of the response payload.
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _get_binary() -> Iterator[bytes]:
            with self._session.get(url,
                                   headers=self._get_headers(
                                       {"Content-Type": None, "Accept": (accept or "*/*")}
                                   ),
                                   verify=self.ssl_config.get_verify(),
                                   cert=self.ssl_config.get_cert(),
                                   timeout=self._timeout,
                                   stream=True,
                                   proxies=self._get_proxies()) as res:
                self._log_communication(res, response_body=False)
                self._check_response(res)
                self._check_status_error(res)

                yield from res.iter_content(chunk_size=65536)

        with _translate_timeout_errors():
            yield from self._retry_generator_on_rate_limit(_get_binary)

    def post_binary(self,
                    url: str,
                    data: Any,
                    content_type: str = None,
                    expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of POST for binary data.

        :param url: Url to use
        :param data: The payload to POST. This can be anything 'requests.post(data=...)' supports.
        :param content_type: The content type of the data. Defaults to "application/octet-stream" internally if unset.
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _post_binary() -> Any:
            res = self._session.post(url,
                                     data=data,
                                     headers=self._get_headers(
                                         {"Content-Type": (content_type or "application/octet-stream")}
                                     ),
                                     verify=self.ssl_config.get_verify(),
                                     cert=self.ssl_config.get_cert(),
                                     timeout=self._timeout,
                                     proxies=self._get_proxies())
            self._log_communication(res, request_body=False)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_post_binary)

    def put_binary(self,
                   url: str,
                   data: Any,
                   content_type: str = None,
                   expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of PUT for binary data.

        :param url: Url to use
        :param data: The payload to PUT. This can be anything 'requests.put(data=...)' supports.
        :param content_type: The content type of the data. Defaults to "application/octet-stream" internally if unset.
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _put_binary() -> Any:
            res = self._session.put(url,
                                    data=data,
                                    headers=self._get_headers(
                                        {"Content-Type": (content_type or "application/octet-stream")}
                                    ),
                                    verify=self.ssl_config.get_verify(),
                                    cert=self.ssl_config.get_cert(),
                                    timeout=self._timeout,
                                    proxies=self._get_proxies())
            self._log_communication(res, request_body=False)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_put_binary)

    def get(self,
            url: str,
            expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of GET

        :param url: Url to use
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _get() -> Any:
            res = self._session.get(url,
                                    headers=self._get_headers({"Content-Type": None}),
                                    verify=self.ssl_config.get_verify(),
                                    cert=self.ssl_config.get_cert(),
                                    timeout=self._timeout,
                                    proxies=self._get_proxies())
            self._log_communication(res)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_get)

    def post(self,
             url: str,
             data: Any,
             expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of POST

        :param url: Url to use
        :param data: The payload to POST
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _post() -> Any:
            res = self._session.post(url,
                                     json=data,
                                     headers=self._get_headers(),
                                     verify=self.ssl_config.get_verify(),
                                     cert=self.ssl_config.get_cert(),
                                     timeout=self._timeout,
                                     proxies=self._get_proxies())
            self._log_communication(res)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_post)

    def put(self,
            url: str,
            data: Any,
            expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of PUT

        :param url: Url to use
        :param data: The payload to PUT
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _put() -> Any:
            res = self._session.put(url,
                                    json=data,
                                    headers=self._get_headers(),
                                    verify=self.ssl_config.get_verify(),
                                    cert=self.ssl_config.get_cert(),
                                    timeout=self._timeout,
                                    proxies=self._get_proxies())
            self._log_communication(res)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_put)

    def patch(self,
              url: str,
              data: Any,
              expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of PATCH

        :param url: Url to use
        :param data: The payload to PUT
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _patch() -> Any:
            res = self._session.patch(url,
                                      json=data,
                                      headers=self._get_headers(),
                                      verify=self.ssl_config.get_verify(),
                                      cert=self.ssl_config.get_cert(),
                                      timeout=self._timeout,
                                      proxies=self._get_proxies())
            self._log_communication(res)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_patch)

    def delete(self,
               url: str,
               expected_media_type: str = 'application/json') -> Any:
        """
        Implementation of DELETE

        :param url: Url to use
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The payload of the response
        """

        @backoff.on_exception(*BACKOFF_ARGS, **BACKOFF_KWARGS, max_tries=self._get_max_tries)
        def _delete() -> Any:
            res = self._session.delete(url,
                                       headers=self._get_headers({"Content-Type": None}),
                                       verify=self.ssl_config.get_verify(),
                                       cert=self.ssl_config.get_cert(),
                                       timeout=self._timeout,
                                       proxies=self._get_proxies())
            self._log_communication(res)
            return self._parse_response(res, expected_media_type)

        with _translate_timeout_errors():
            return self._retry_on_rate_limit(_delete)

    ###############################################################################################################
    # Tool methods for requests
    ###############################################################################################################

    def _get_proxies(self) -> dict:
        """
        Create a copy of proxies if they exist or return None

        :return: copy of self._proxies or None
        """
        return self._proxies.copy() if self._proxies else None

    @staticmethod
    def _merge_headers(headers: dict, override: dict) -> dict:
        """
        Merge headers with override.

        :param headers: Headers to merge into.
        :param override: Dict of headers that override *headers*. If a header key is set to value None,
               it will be removed from *headers*.
        :return: The merged headers.
        """
        if isinstance(headers, dict) and isinstance(override, dict):
            headers.update({AbstractAPI._capitalize_header(k): v for k, v in override.items()})
            headers = {k: v for k, v in headers.items() if v is not None}

        return headers

    def _get_headers(self, override: dict = None) -> dict:
        """
        Create a header dict for requests. Uses abstract method *self._handle_token()*.

        :param override: Dict of headers that override the internal headers. If a header key is set to value None,
               it will be removed from the headers.
        :return: A dict containing header values for requests.
        """

        headers = AbstractAPI._merge_headers(self._headers.copy(), override)

        token = self._handle_token()
        if token:
            headers['Authorization'] = "Bearer " + token

        return headers

    @staticmethod
    def _bool_to_external_str(value: Any) -> Optional[str]:
        """
        Translate bool to string values "true" and "false" if value is a bool.

        :param value: The value to check.
        :return: String representation of the value or None.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def _get_query_part(params: dict) -> str:
        """
        Create the query part of an url. Keys in *params* whose values are set to None are removed.

        :param params: A dict of params to use for the query.
        :return: The query part of an url with a leading '?', or an empty string when query is empty.
        """
        params_cleaned = {k: AbstractAPI._bool_to_external_str(v) for k, v in params.items() if v is not None}
        return ('?' + urlencode(params_cleaned, quote_via=quote, safe="/,")) if params_cleaned else ""

    def _wait_for_rate_limit(self, err: "HiroRateLimitError", attempt: int) -> None:
        """
        Sleep before repeating a request that has been rejected with status 429.

        :param err: The rate limit error. Its *retry_after* is used as wait time, with an exponential fallback.
        :param attempt: Number of the retry that is about to happen, starting at 1.
        :raises HiroRateLimitError: *err* itself, when all retries are used up or the wait exceeds the configured cap.
        """
        if attempt > self._rate_limit_max_retries:
            raise err

        wait = err.retry_after if err.retry_after is not None else 2 ** (attempt - 1)

        if wait > self._rate_limit_max_wait:
            raise err

        wait = jittered_wait(wait, cap=self._rate_limit_max_wait)

        logger.warning("Rate limited by %s (policy: %s). Retry %d/%d in %.1fs.",
                       err.response.url if err.response is not None else "backend",
                       err.policy, attempt, self._rate_limit_max_retries, wait)
        time.sleep(wait)

    def _retry_on_rate_limit(self, func):
        """
        Call *func* and repeat it as long as the backend answers with status 429, see *_wait_for_rate_limit()*.

        :param func: A function without parameters that performs the request.
        :return: The result of *func*.
        """
        attempt = 0
        while True:
            try:
                return func()
            except HiroRateLimitError as err:
                attempt += 1
                self._wait_for_rate_limit(err, attempt)

    def _retry_generator_on_rate_limit(self, func):
        """
        Like *_retry_on_rate_limit()*, for a generator function. A request is only repeated as long as nothing
        has been yielded yet.

        :param func: A generator function without parameters.
        """
        attempt = 0
        while True:
            started = False
            try:
                for item in func():
                    started = True
                    yield item
                return
            except HiroRateLimitError as err:
                if started:
                    raise
                attempt += 1
                self._wait_for_rate_limit(err, attempt)

    def _parse_response(self,
                        res: requests.Response,
                        expected_media_type: str = 'application/json') -> Any:
        """
        Parse the response of the backend.

        :param res: The result payload
        :param expected_media_type: The expected media type. Default is 'application/json'. If this is set to '*' or
               '*/*', any media_type is accepted.
        :return: The result payload. A json type when the result media_type within Content-Type is 'application/json'
                 (usually a dict), a str otherwise.
        :raises RequestException: On HTTP errors.
        :raises WrongContentTypeError: When the Media-Type of the Content-Type of the Response is not
                *expected_media_type*.
        """
        try:
            self._check_response(res)
            self._check_status_error(res)
            if expected_media_type not in ['*', '*/*']:
                AbstractAPI._check_content_type(res, expected_media_type)
            if expected_media_type.lower() == 'application/json':
                return res.json()
            else:
                return str(res.text)
        except (json.JSONDecodeError, ValueError):
            return {"error": {"message": res.text, "code": 999}}

    def _log_communication(self, res: requests.Response, request_body: bool = True, response_body: bool = True) -> None:
        """
        Log communication that flows across requests' methods. Contains options to disable logging of the body portions
        of the requests to avoid dumping large amounts of data and breaking streaming of results.

        This log does not log an exact binary representation of the transmitted bodies but uses the encoding defined
        in *res* to print it as strings.

        Authorization and cookie headers will be obscured by only displaying the last six characters of their values.

        :param res: The response of a request. Also contains the request.
        :param request_body: Option to disable the logging of the request_body.
        :param response_body: Option to disable the logging of the response_body.
        """

        def _log_headers(headers) -> str:
            result: str = ""
            for k, v in headers.items():
                cap_key: str = self._capitalize_header(k)
                if cap_key == "Authorization" or cap_key.find("Cookie") != -1:
                    v = f"{v[:6]}[{len(v) - 12} characters hidden]{v[-6:]}"
                result += f"{k}: {v}\n"
            return result

        def _body_str(body: Union[str, bytes], encoding: str) -> str:
            if body is None:
                return ""
            if isinstance(body, bytes):
                body = str(body, encoding or 'utf8')
            return body

        ok = self._check_response_ok(res)

        if (not ok and self._log_communication_on_error) or logger.isEnabledFor(logging.DEBUG):
            log_message = f'''
################ request ################
{res.request.method} {res.request.url}
{_log_headers(res.request.headers)}
{_body_str(res.request.body, res.encoding) if request_body else "(body hidden)"}
################ response ################
{res.status_code} {res.reason} {res.url}
{_log_headers(res.headers)}
{_body_str(res.text, res.encoding) if response_body else "(body hidden)"}
'''

            if not ok:
                logger.error(log_message)
            else:
                logger.debug(log_message)

    def _check_status_error(self, res: requests.Response) -> None:
        """
        Catch exceptions and rethrow them with additional information returned by the error response body.

        :param res: The response
        :raises requests.exceptions.HTTPError: When an HTTPError occurred.
        """
        try:
            if self._raise_exceptions:
                res.raise_for_status()
                if res.status_code > 600:
                    raise requests.exceptions.HTTPError(
                        u'%s Illegal return code: %s for url: %s' % (res.status_code, res.reason, res.url),
                        response=res)

        except requests.exceptions.HTTPError as err:
            http_error_msg = str(err.args[0])

            if res.content:
                try:
                    AbstractAPI._check_content_type(res, 'application/json')
                    http_error_msg += ": " + self._get_error_message(res.json())
                except (json.JSONDecodeError, KeyError, WrongContentTypeError):
                    if '_TOKEN' not in res.text:
                        http_error_msg += ": " + str(res.text)

            if res.status_code == 429:
                raise HiroRateLimitError(http_error_msg, response=err.response) from err

            raise requests.exceptions.HTTPError(http_error_msg, response=err.response) from err

    def _get_error_message(self, json_result: dict) -> str:
        """
        Extract error message from {"error": { "message": "(errormessage)" }} or {"error":"(errormessage)" } if
        possible. Return the complete JSON as str if not.

        Child API classes should overwrite this method if their error messages differ.

        :param json_result: The json dict containing the error message.
        :return: The extracted error message.
        """
        if 'error' in json_result:
            error_block = json_result.get('error')
            if isinstance(error_block, dict):
                error_message = error_block.get('message')
                return error_message if isinstance(error_message, str) else json.dumps(error_block)
            if isinstance(error_block, str):
                return error_block

        return json.dumps(json_result)

    @staticmethod
    def _check_content_type(res: requests.Response, expected_media_type: str) -> None:
        """
        Raise an Exception if the Content-Type header of the response does not contain the *expected_media_type*.
        Compares only the media-type portion of the header (by splitting at ';').

        :param res: The response object
        :param expected_media_type: The expected Media-Type.
        :raise WrongContentTypeError: When the Media-Type of the Content-Type is not *expected_media_type* or
               the header is missing completely.
        """
        content_type = res.headers.get('Content-Type')
        if not content_type:
            raise WrongContentTypeError("Response has no Content-Type.")

        media_type = content_type.lower().split(';')[0]

        if media_type != expected_media_type.lower():
            raise WrongContentTypeError(
                f"Expected media-type '{expected_media_type}' in Content-Type header, but got '{media_type}'."
            )

    def _check_response_ok(self, res: requests.Response) -> bool:
        """
        Do not rely on res.ok. Everything not between 200 and 399 is an error.

        Child API classes should overwrite this method if other custom and valid status codes might occur there.

        :param res: The response object
        :return: True on good response, false otherwise.
        """
        return 200 <= res.status_code < 400

    ###############################################################################################################
    # Response and token handling
    # Child classes have to override those classes for special handling like token and header handling.
    ###############################################################################################################

    def _check_response(self, res: requests.Response) -> None:
        """
        Root method. No response checking here.

        :param res: The result payload
        """
        return

    def _handle_token(self) -> Optional[str]:
        """
        Just return None, therefore a header without Authorization
        will be created in *self._get_headers()*.

        Does *not* try to obtain or refresh a token.

        :return: Always None here, derived classes should return a token string.
        """
        return None


###################################################################################################################
# ConnectionHandler class
###################################################################################################################

class GraphConnectionHandler(AbstractAPI):
    """
    Contains information about a Graph Connection. This class also handles resolving the current api endpoints.
    Also creates the requests.Session which will be shared among the API Modules using this connection.
    """

    _pool_maxsize = 10
    """Default pool_maxsize for requests.adapters.HTTPAdapter."""

    _pool_block = False
    """As used by requests.adapters.HTTPAdapter."""

    _version_info: dict = None
    """Stores the result of /api/version"""

    custom_endpoints: dict = None
    """Override API endpoints."""

    _lock: threading.RLock
    """Reentrant mutex for thread safety"""

    def __init__(self,
                 root_url: str = None,
                 custom_endpoints: dict = None,
                 version_info: dict = None,
                 pool_maxsize: int = None,
                 pool_block: bool = None,
                 connection_handler=None,
                 *args,
                 **kwargs):
        """
        Constructor

        Example for custom_endpoints (see params below):

        ::

           {
               "graph": "/api/graph/7.2",
               "auth": "/api/auth/6.2",
               "action-ws": ("/api/action-ws/1.0", "action-1.0.0")
           }

        This object creates the *requests.Session* and *requests.adapters.HTTPAdapter* for this *root_url*. The
        *pool_maxsize* of such a session can be set via the parameter in the constructor. When a TokenApiHandler is
        shared between different API objects (like HiroGraph, HiroApp, etc.), this session and its pool are also
        shared.

        See parent :class:`AbstractAPI` for a description of all remaining parameters.

        :param root_url: Root url for HIRO, like https://core.engine.datagroup.de.
        :param custom_endpoints: Optional map of {name:endpoint_path, ...} that overrides or adds to the endpoints taken
               from /api/version. Example see above.
        :param version_info: Optional full dict of the JSON result received via /api/version. Setting this will use it
               as the valid API version information and avoids the internal API-call altogether.
        :param pool_maxsize: Size of a connection pool for a single connection. See requests.adapters.HTTPAdapter.
               Default is 10. *pool_maxsize* is ignored when *session* is set.
        :param pool_block: Block any connections that exceed the pool_maxsize. Default is False: Allow more connections,
               but do not cache them. See requests.adapters.HTTPAdapter. *pool_block* is ignored when *session* is set.
        :param connection_handler: Copy parameters from this already existing connection handler. Overrides all other
               parameters.
        :param args: Unnamed parameter passthrough for parent class.
        :param kwargs: Named parameter passthrough for parent class.
        """
        self._lock = threading.RLock()

        if isinstance(connection_handler, GraphConnectionHandler):
            root_url = connection_handler._root_url
            session = connection_handler._session
            custom_endpoints = connection_handler.custom_endpoints
            version_info = connection_handler._version_info
        else:
            if not root_url:
                raise ValueError("'root_url' must not be empty.")

            adapter = requests.adapters.HTTPAdapter(
                pool_maxsize=pool_maxsize or self._pool_maxsize,
                pool_connections=1,
                pool_block=pool_block or self._pool_block
            )
            session = requests.Session()
            session.mount(prefix=root_url, adapter=adapter)

        super().__init__(
            root_url=root_url,
            session=session,
            abstract_api=connection_handler,
            *args,
            **kwargs
        )

        self.custom_endpoints = custom_endpoints
        self._version_info = version_info

        self.get_version()

    @staticmethod
    def _remove_slash(endpoint: str) -> str:
        return endpoint[:-1] if endpoint[-1] == '/' else endpoint

    ###############################################################################################################
    # Public methods
    ###############################################################################################################

    def get_api_endpoint_of(self, api_name: str) -> str:
        """
        Determines endpoints of the API names.
        Loads and caches the current API information if necessary.

        :param api_name: Name of the HIRO API
        :return: Full url of endpoint for this API
        """

        if self.custom_endpoints:
            endpoint = self.custom_endpoints.get(api_name)
            if endpoint:
                return self._remove_slash(self._root_url + endpoint)

        api_entry: dict = self.get_version().get(api_name)

        if not api_entry:
            raise ValueError("No API named '{}' found.".format(api_name))

        return self._remove_slash(self._root_url + api_entry.get('endpoint'))

    def get_websocket_config(self, api_name: str) -> Tuple[
        str,
        str,
        Optional[str],
        Optional[int],
        Optional[dict]
    ]:
        """
        Determines endpoints for websockets of the API names.
        Loads and caches the current API information if necessary.
        If proxies have been given, the key of the proxy picked needs to be "ws" or "wss" respectively.

        :param api_name: Name of the HIRO API for websockets
        :return: Tuple of full url of websocket for this API, its protocol, its proxy_host, its proxy port and proxy
                 auth (if any).
        """

        def _get_proxy_info(_url: str) -> Tuple[Optional[str], Optional[int], Optional[dict]]:
            proxies = self._get_proxies()
            if not proxies:
                return None, None, None

            url_parts = urllib.parse.urlparse(_url)
            proxy_url = proxies.get(url_parts.scheme)

            if not proxy_url:
                return None, None, None

            proxy_url_parts = urllib.parse.urlparse(proxy_url)

            proxy_auth: dict = {
                proxy_url_parts.username: proxy_url_parts.password
            } if proxy_url_parts.username else None

            return proxy_url_parts.hostname, proxy_url_parts.port, proxy_auth

        def _construct_result(_endpoint: str, _protocol: str) -> Tuple[
            str,
            str,
            Optional[str],
            Optional[str],
            Optional[dict]
        ]:
            _url: str = self._root_url.lower().replace('https://', 'wss://').replace('http://', 'ws://')
            _proxy, _proxy_port, _proxy_auth = _get_proxy_info(_url)
            return self._remove_slash(_url + _endpoint), _protocol, _proxy, _proxy_port, _proxy_auth

        if self.custom_endpoints:
            value = self.custom_endpoints.get(api_name)
            if isinstance(value, tuple):
                endpoint, protocol = value
                if endpoint:
                    return _construct_result(endpoint, protocol)

        api_entry: dict = self.get_version().get(api_name)

        if not api_entry:
            raise ValueError("No WS API named '{}' found.".format(api_name))

        return _construct_result(api_entry.get('endpoint'), api_entry.get('protocol'))

    ###############################################################################################################
    # REST API operations
    ###############################################################################################################

    def get_version(self, force_update: bool = False) -> dict:
        """
        HIRO REST query API: `GET self._endpoint + '/api/version'`

        :param force_update: Force updating the internal cache with version_info via API request.
        :return: The result payload
        """
        with self._lock:
            if not self._version_info or force_update:
                url = self._root_url + '/api/version'
                self._version_info = self.get(url)

            return self._version_info


###################################################################################################################
# TokenApiHandler classes
###################################################################################################################

class AbstractTokenApiHandler(GraphConnectionHandler):
    """
    Root class for all TokenApiHandler classes. This adds token handling.
    """

    def __init__(self, *args, **kwargs):
        """
        Constructor

        See parent :class:`GraphConnectionHandler` for a full description
        of all remaining parameters.

        :param args: Unnamed parameter passthrough for parent class.
        :param kwargs: Named parameter passthrough for parent class.
        """
        super().__init__(*args, **kwargs)

    ###############################################################################################################
    # Token handling
    ###############################################################################################################

    @property
    def token(self) -> str:
        """
        Return the current token.
        """
        raise RuntimeError('Cannot use property of this abstract class.')

    def decode_token(self) -> dict:
        """
        Return a dict with the decoded token payload from the internal token. This payload contains detailed
        information about what this token has access to.

        :return: The dict with the decoded token payload.
        :raises AuthenticationTokenError: When the token does not contain the base64 encoded data payload.
        """
        return AbstractTokenApiHandler.decode_token_ext(self.token)

    @staticmethod
    def decode_token_ext(token: str) -> dict:
        """
        Return a dict with the decoded token payload. This payload contains detailed information about what this token
        has access to.

        :param token: The token to decode.
        :return: The dict with the decoded token payload.
        :raises AuthenticationTokenError: When the token does not contain the base64 encoded data payload.
        """
        base64_payload: list = token.split('.')
        if len(base64_payload) == 1:
            raise AuthenticationTokenError("Token is missing base64 payload")

        payload = base64_payload[1] + '=' * (4 - len(base64_payload[1]) % 4)

        json_payload = base64.urlsafe_b64decode(payload)

        return dict(json.loads(json_payload))

    @abstractmethod
    def refresh_token(self) -> None:
        """
        Refresh the current token.
        """
        raise RuntimeError('Cannot use method of this abstract class.')

    @abstractmethod
    def revoke_token(self, token_hint: str = "revoke_token") -> None:
        """
        Revoke a token.

        :param token_hint: The default is to revoke the "revoke_token". The alternative is "access_token".
        """
        raise RuntimeError('Cannot use method of this abstract class.')

    @abstractmethod
    def refresh_time(self) -> Optional[int]:
        """
        Calculate the time after which the token should be refreshed in milliseconds.

        :return: The timestamp in ms after which the token shall be refreshed or None if the token cannot be refreshed
                 on its own.
        """
        raise RuntimeError('Cannot use method of this abstract class.')


class FixedTokenApiHandler(AbstractTokenApiHandler):
    """
    TokenApiHandler for a fixed token.
    """

    _token: str
    """Stores the fixed token."""

    def __init__(self, token: str = None, *args, **kwargs):
        """
        Constructor

        See parent :class:`AbstractTokenApiHandler` for a full description
        of all remaining parameters.

        :param token: The fixed token for the HTTP requests.
        :param args: Unnamed parameter passthrough for parent class. See :class:`AbstractTokenApiHandler`.
        :param kwargs: Named parameter passthrough for parent class. See :class:`AbstractTokenApiHandler`.
        """
        super().__init__(*args, **kwargs)

        self._token = token

    @property
    def token(self) -> str:
        return self._token

    def refresh_token(self) -> None:
        raise FixedTokenError('Token is invalid and cannot be changed because it has been given externally.')

    def revoke_token(self, token_hint: str = "revoke_token") -> None:
        raise FixedTokenError('Token cannot be revoked because it has been given externally.')

    def refresh_time(self) -> Optional[int]:
        """

        :return: Always none
        """
        return None


class EnvironmentTokenApiHandler(AbstractTokenApiHandler):
    """
    TokenApiHandler for a fixed token given as an environment variable.
    """

    _env_var: str
    """Stores the name of the environment variable."""

    def __init__(self, env_var: str = 'HIRO_TOKEN', *args, **kwargs):
        """
        Constructor

        See parent :class:`AbstractTokenApiHandler` for a full description
        of all remaining parameters.

        :param env_var: Name of the environment variable with the token.
        :param args: Unnamed parameter passthrough for parent class.
        :param kwargs: Named parameter passthrough for parent class. 
        """
        super().__init__(*args, **kwargs)

        self._env_var = env_var

    @property
    def token(self) -> str:
        return os.environ[self._env_var]

    def refresh_token(self) -> None:
        raise FixedTokenError(
            "Token is invalid and cannot be changed because it has been given as environment variable '{}'"
            " externally.".format(self._env_var))

    def revoke_token(self, token_hint: str = "revoke_token") -> None:
        raise FixedTokenError(
            "Token cannot be revoked because it has been given as environment variable '{}'"
            " externally.".format(self._env_var))

    def refresh_time(self) -> Optional[int]:
        """

        :return: Always none
        """
        return None


class TokenInfo:
    """
    This class stores token information from the auth api.
    """

    token: str = None
    """ The token string """
    expires_at = -1
    """ Token expiration in ms since epoch"""
    refresh_token: str = None
    """ The refresh token to use - if any."""
    last_update = 0
    """ Timestamp of when the token has been fetched in ms."""
    refresh_offset = 5000
    """ Milliseconds of offset for token expiry """

    def __init__(self, token: str = None, refresh_token: str = None, expires_at: int = -1, refresh_offset: int = 5000):
        """
        Constructor

        :param token: The token string
        :param refresh_token: A refresh token
        :param expires_at: Token expiration in ms since epoch
        :param refresh_offset: Offset in milliseconds that will be subtracted from the expiry time so a token will be
                               refreshed in time. Default is 5 seconds.
        """
        self.token = token
        self.expires_at = expires_at
        self.refresh_token = refresh_token
        self.last_update = self.get_epoch_millis() if token else 0
        self.refresh_offset = refresh_offset

    @staticmethod
    def get_epoch_millis() -> int:
        """
        Get timestamp

        :return: Current epoch in milliseconds
        """
        return int(time.time_ns() / 1000000)

    def parse_token_result(self, res: dict, what: str) -> None:
        """
        Parse the result payload and extract token information.

        :param res: The result payload from the backend.
        :param what: What token command has been issued (for error messages).
        :raises TokenUnauthorizedError: When the token request returned error 401. This usually means, that this token
                has expired.
        :raises AuthenticationTokenError: When the token request returned any other error.
        """
        if 'error' in res:
            message: str = '{}: {}'.format(what, res['error'].get('message'))
            code: int = int(res['error'].get('code'))

            if code == 401:
                raise TokenUnauthorizedError(message, code)
            else:
                raise AuthenticationTokenError(message, code)

        self.last_update = self.get_epoch_millis()

        self.token = res.get('_TOKEN') or res.get('access_token')

        expires_at = res.get('expires-at')
        if expires_at:
            self.expires_at = int(expires_at)
        else:
            expires_in = res.get('expires_in')
            if expires_in:
                self.expires_at = self.last_update + int(expires_in) * 1000

        refresh_token = res.get('refresh_token')
        if refresh_token:
            self.refresh_token = refresh_token

    def expired(self) -> bool:
        """
        Check token expiration.

        :return: True when the token has been expired *(expires_at - refresh_offset) <= get_epoch_mills()*. If
                 no *expires_at* is available, always return False since this token would never expire.
        """
        if self.refresh_time() is None:
            return False

        return self.refresh_time() <= self.get_epoch_millis()

    def refresh_time(self) -> Optional[int]:
        """
        Calculate the time after which the token should be refreshed in milliseconds.

        :return: expires_at - refresh_offset (in ms) or None if refresh is not possible.
        """
        return self.expires_at - self.refresh_offset if self.expires_at > 0 else None

    def clear_token_data(self, access_token_only: bool):
        """
        Handle internal data on a token revoke.

        :param access_token_only: True if only the access token gets revoked.
        """
        del self.token
        self.last_update = self.get_epoch_millis()
        if not access_token_only:
            del self.refresh_token
            self.expires_at = -1


class PasswordAuthTokenApiHandler(AbstractTokenApiHandler):
    """
    API Tokens will be fetched using this class. It does not handle any automatic token fetching, refresh or token
    expiry. This has to be checked and triggered by the *caller*.

    The methods of this class are thread-safe, so it can be shared between several HIRO objects.

    It is built this way to avoid endless calling loops when resolving tokens.
    """

    _token_info: TokenInfo = None
    """Contains all token information"""

    _lock: threading.RLock
    """Reentrant mutex for thread safety"""

    _username: str
    _password: str
    _client_id: str
    _client_secret: str

    _secure_logging: bool = True
    """Avoid logging of sensitive data."""

    def __init__(self,
                 username: str = None,
                 password: str = None,
                 client_id: str = None,
                 client_secret: str = None,
                 secure_logging: bool = True,
                 *args, **kwargs):
        """
        Constructor

        See parent :class:`AbstractTokenApiHandler` for a full description
        of all remaining parameters.

        :param username: Username for authentication
        :param password: Password for authentication
        :param client_id: OAuth client_id for authentication
        :param client_secret: OAuth client_secret for authentication
        :param secure_logging: If this is enabled, payloads that might contain sensitive information are not logged.
        :param args: Unnamed parameter passthrough for parent class. 
        :param kwargs: Named parameter passthrough for parent class. 
        """
        super().__init__(*args, **kwargs)

        self._username = username
        self._password = password
        self._client_id = client_id
        self._client_secret = client_secret

        self._secure_logging = secure_logging

        self._token_info = TokenInfo()
        self._lock = threading.RLock()

    @property
    def endpoint(self):
        return self.get_api_endpoint_of('auth')

    @property
    def token(self) -> str:
        """Get the token. Get or refresh it if necessary."""
        with self._lock:
            if not self._token_info.token:
                self.get_token()
            elif self._token_info.expired():
                self.refresh_token()

            return self._token_info.token

    def _log_communication(self, res: requests.Response, request_body: bool = True, response_body: bool = True) -> None:
        """
        Logging under a secure aspect. Hides sensitive information unless *self._secure_logging* is set to False.

        :param res: The response of a request. Also contains the request.
        :param request_body: Option to disable the logging of the request_body. If set to True, will only remain True
               internally when *self._secure_logging* is set to False.
        :param response_body: Option to disable the logging of the response_body.  If set to True, will only remain True
               internally when *self._secure_logging* is set to False or *res.status_code* != 200.
        """
        log_request_body = not self._secure_logging and request_body is True
        log_response_body = (res.status_code != 200 or not self._secure_logging) and response_body is True

        super()._log_communication(res, request_body=log_request_body, response_body=log_response_body)

    def get_token(self) -> None:
        """
        Construct a request to obtain a new token. API self._endpoint + '/app'

        :raises AuthenticationTokenError: When no auth_endpoint is set.
        """
        with self._lock:
            if not self.endpoint:
                raise AuthenticationTokenError(
                    'Token is invalid and endpoint (auth_endpoint) for obtaining is not set.')

            if not self._username or not self._password or not self._client_id or not self._client_secret:
                msg = ""
                if not self._username:
                    msg += "'username'"
                if not self._password:
                    msg += (", " if msg else "") + "'password'"
                if not self._client_id:
                    msg += (", " if msg else "") + "'client_id'"
                if not self._client_secret:
                    msg += (", " if msg else "") + "'client_secret'"
                raise AuthenticationTokenError(
                    "{} is missing required parameter(s) {}.".format(self.__class__.__name__, msg))

            url = self.endpoint + '/app'
            data = {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "username": self._username,
                "password": self._password
            }

            res = self.post(url, data)
            self._token_info.parse_token_result(res, "{}.get_token".format(self.__class__.__name__))

    def refresh_token(self) -> None:
        """
        Construct a request to refresh an existing token. API self._endpoint + '/refresh'.

        :raises AuthenticationTokenError: When no auth_endpoint is set.
        """
        with self._lock:
            if not self.endpoint:
                raise AuthenticationTokenError(
                    'Token is invalid and endpoint (auth_endpoint) for refresh is not set.')

            if not self._token_info.refresh_token:
                self.get_token()
                return

            url = self.endpoint + '/refresh'
            data = {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._token_info.refresh_token
            }

            try:
                res = self.post(url, data)
                self._token_info.parse_token_result(res, "{}.refresh_token".format(self.__class__.__name__))
            except AuthenticationTokenError:
                self.get_token()

    def revoke_token(self, token_hint: str = "refresh_token") -> None:
        """
        Revoke a token.

        :param token_hint: The default is to revoke the "revoke_token". The alternative is "access_token".
                           (has effect after auth api version 6.6)
        """
        with self._lock:
            if not self.endpoint:
                raise AuthenticationTokenError(
                    'Token is invalid and endpoint (auth_endpoint) for revoke is not set.')

            auth_api_version = float(self._version_info['auth']['version'])
            url = self.endpoint + '/revoke'

            if auth_api_version >= 6.6:
                if token_hint == "access_token":
                    token = self._token_info.token
                elif token_hint == "refresh_token":
                    token = self._token_info.refresh_token
                else:
                    raise AuthenticationTokenError(f"token_hint '{token_hint}' is wrong", 400)

                data = {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "token": token,
                    "token_hint": token_hint
                }
            else:
                token_hint = "refresh_token"
                data = {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._token_info.refresh_token
                }

            self.post(url, data)

            self._token_info.clear_token_data(token_hint != "refresh_token")

    def refresh_time(self) -> Optional[int]:
        """
        Calculate refresh time.

        :return: Timestamp after which the token becomes invalid. Returns None if token cannot be refreshed.
        """
        return self._token_info.refresh_time()

    ###############################################################################################################
    # Response and token handling
    ###############################################################################################################

    def _check_response(self, res: requests.Response) -> None:
        """
        Response checking. When a refresh_token is present and status_code is 401, raise TokenUnauthorizedError.
        This can happen when a refresh-token is not valid anymore.

        :param res: The result payload
        :raise TokenUnauthorizedError: Raised to trigger a retry via self.get_token() in self.refresh_token().
        """
        if res.status_code == 401 and self._token_info.refresh_token:
            raise TokenUnauthorizedError(str(res.text), 401)

    def _handle_token(self) -> Optional[str]:
        """
        Just return None, therefore a header without Authorization
        will be created in *self._get_headers()*.

        Does *not* try to obtain or refresh a token.

        :return: *token* given.
        """
        return None


###################################################################################################################
# Root class for different API groups
###################################################################################################################

class AuthenticatedAPIHandler(AbstractAPI):
    """
    Python implementation for accessing a REST API with authentication.
    """

    _api_handler: AbstractTokenApiHandler
    """Stores the TokenApiHandler used for this API."""

    _api_name: str
    """Name of the API."""

    def __init__(self,
                 api_handler: AbstractTokenApiHandler,
                 api_name: str):
        """
        Constructor

        :param api_name: Name of the API to use.
        :param api_handler: External API handler.
        """
        if not api_handler or not api_name:
            raise ValueError("Cannot authenticate against HIRO without *api_handler* and *api_name*.")

        super().__init__(abstract_api=api_handler)

        self._api_handler = api_handler
        self._api_name = api_name

    @property
    def endpoint(self) -> str:
        return self._api_handler.get_api_endpoint_of(self._api_name)

    ###############################################################################################################
    # Response and token handling
    ###############################################################################################################

    def _check_response(self, res: requests.Response) -> None:
        """
        Response checking. Tries to refresh the token on status_code 401, then raises RequestException to try
        again using backoff.

        :param res: The result payload
        :raises requests.exceptions.RequestException: When an error 401 occurred and the token has been refreshed.
        """
        if res.status_code == 401:
            self._api_handler.refresh_token()

            # Raise this exception to trigger retry with backoff
            raise requests.exceptions.RequestException

    def _handle_token(self) -> Optional[str]:
        """
        Try to return a valid token by obtaining or refreshing it.

        :return: A valid token.
        """
        return self._api_handler.token


###################################################################################################################
# Exceptions
###################################################################################################################

class AuthenticationTokenError(Exception):
    """
    Class for unrecoverable failures with access tokens.
    Contains a message and an optional message code. If the code is None, no code will be printed in __str__().
    """
    message: str
    code: int

    def __init__(self, message: str, code: int = None):
        self.message = message
        self.code = code

    def __str__(self) -> str:
        if self.code is None:
            return "{}: {}".format(self.__class__.__name__, self.message)
        else:
            return "{}: {} ({})".format(self.__class__.__name__, self.message, self.code)


class TokenUnauthorizedError(AuthenticationTokenError):
    """
    Child of *AuthenticationTokenErrors*. Used when tokens expire with error 401.
    """
    pass


class FixedTokenError(AuthenticationTokenError):
    """
    Child of *AuthenticationTokenErrors*. Used when tokens are fixed and cannot be refreshed.
    """
    pass


class WrongContentTypeError(Exception):
    """
    When the Content-Type of the result is unexpected, i.e. 'application/json' was expected, but something else got
    returned.
    """
    pass


class HiroRateLimitError(requests.exceptions.HTTPError):
    """
    The backend rejected the request with status 429 (Too Many Requests) and the request could not be completed by
    waiting and retrying (see *rate_limit_max_retries* and *rate_limit_max_wait* of *AbstractAPI*).

    It is an *HTTPError*, so existing handlers for that keep working.
    """

    @property
    def retry_after(self) -> Optional[float]:
        """Seconds the backend asked to wait (header *Retry-After*), or None."""
        return parse_retry_after(self._header('Retry-After'))

    @property
    def policy(self) -> Optional[str]:
        """Raw value of the header *RateLimit-Policy*, or None."""
        return self._header('RateLimit-Policy')

    @property
    def ratelimit(self) -> Optional[str]:
        """Raw value of the header *RateLimit*, or None."""
        return self._header('RateLimit')

    def _header(self, name: str) -> Optional[str]:
        return self.response.headers.get(name) if self.response is not None else None


class HiroTimeoutError(requests.exceptions.Timeout, TimeoutError):
    """
    Raised when a request times out. Catch this to handle *any* timeout.

    *requests* uses a different exception class depending on *when* a timeout occurs: *ConnectTimeout* while connecting,
    *ReadTimeout* while waiting for the response headers, but a plain *ConnectionError* when the timeout happens while
    the response body is being read. That last one is not even a *requests.exceptions.Timeout*, which made it easy to
    miss a timeout in error handling. All of them are raised as a subclass of this class instead.

    This inherits from both :class:`requests.exceptions.Timeout` and the builtin :class:`TimeoutError`, so all of the
    following catch it::

        except HiroTimeoutError            # explicit
        except TimeoutError                # builtin
        except requests.exceptions.Timeout # remains compatible with existing error handling

    The subclasses below additionally keep the original *requests* class, so error handling that catches a specific
    *requests* exception keeps working unchanged.
    """
    pass


class HiroConnectTimeoutError(requests.exceptions.ConnectTimeout, HiroTimeoutError):
    """Raised when the connection to the server could not be established within the connect timeout."""
    pass


class HiroReadTimeoutError(requests.exceptions.ReadTimeout, HiroTimeoutError):
    """Raised when the server did not send the response headers within the read timeout."""
    pass


class HiroBodyTimeoutError(requests.exceptions.ConnectionError, HiroTimeoutError):
    """
    Raised when the server stopped sending data while the response body was being read.

    *requests* reports this as a plain *ConnectionError*, which is why this keeps *ConnectionError* as a base class.
    """
    pass
