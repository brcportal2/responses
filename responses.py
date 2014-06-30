"""
Copyright 2013 Dropbox, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import (
    absolute_import, print_function, division, unicode_literals
)

import re

import six
if six.PY2:
    try:
        from six import cStringIO as BufferIO
    except ImportError:
        from six import StringIO as BufferIO
else:
    from io import BytesIO as BufferIO

from collections import namedtuple, Sequence, Sized, Iterator
from functools import wraps
from urlparse import urlparse, parse_qsl
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError
try:
    from requests.packages.urllib3.response import HTTPResponse
except ImportError:
    from urllib3.response import HTTPResponse

Call = namedtuple('Call', ['request', 'response'])


class CallList(Sequence, Sized):
    def __init__(self):
        self._calls = []

    def __iter__(self):
        return iter(self._calls)

    def __len__(self):
        return len(self._calls)

    def __getitem__(self, idx):
        return self._calls[idx]

    def add(self, request, response):
        self._calls.append(Call(request, response))

    def reset(self):
        self._calls = []


class Request(object):
    '''
    Simple object that is used to pass request info to side_effect callbacks.
    '''

    def __init__(self, method, url, headers=None, params=None, data=None):
        self.method = method
        self.url = url
        self.headers = headers
        self.params = params
        self.data = data

    @classmethod
    def _from_prepared_request(cls, request):
        '''
        Create a request from requests PreparedRequest object
        '''
        parsed_url = urlparse(request.url)
        return Request(
            request.method,
            request.url,
            headers=request.headers,
            params=dict(parse_qsl(parsed_url.query)),
            data=dict(parse_qsl(request.body)),
        )


class Response(object):
    def __init__(self, body='', status=200, adding_headers=None,
                 stream=False, content_type='text/plain'):

        # body must be bytes
        if isinstance(body, six.text_type):
            body = body.encode('utf-8')

        self.body = body
        self.status = status
        self.adding_headers = adding_headers
        self.stream = stream
        self.content_type = content_type

    def _as_http_response(self):
        headers = {
            'Content-Type': self.content_type,
        }
        if self.adding_headers:
            headers.update(self.adding_headers)

        return HTTPResponse(
            status=self.status,
            body=BufferIO(self.body),
            headers=headers,
            preload_content=False,
        )

    def _as_requests_response(self, request):
        adapter = HTTPAdapter()

        response = adapter.build_response(request, self._as_http_response())
        if not self.stream:
            response.content  # NOQA

        return response


class RequestsMock(object):
    DELETE = 'DELETE'
    GET = 'GET'
    HEAD = 'HEAD'
    OPTIONS = 'OPTIONS'
    PATCH = 'PATCH'
    POST = 'POST'
    PUT = 'PUT'

    def __init__(self):
        self._calls = CallList()
        self.reset()

    def reset(self):
        self._urls = []
        self._calls.reset()

    def add(self, method, url, match_querystring=False, side_effect=None,
            *args, **kwargs):

        response = Response(*args, **kwargs)

        # ensure the url has a default path set
        if url.count('/') == 2:
            url = url.replace('?', '/?', 1) if match_querystring \
                else url + '/'

        # Get iterator of a container
        if isinstance(side_effect, Sequence):
            side_effect = iter(side_effect)

        match = {
            'url': url,
            'method': method,
            'match_querystring': match_querystring,
            'side_effect': side_effect,
            'response': response,
        }

        self._urls.append(match)

    @property
    def calls(self):
        return self._calls

    def activate(self, func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            self.start()
            try:
                return func(*args, **kwargs)
            finally:
                self.stop()
                self.reset()
        return wrapped

    def _find_match(self, request):
        url = request.url
        url_without_qs = url.split('?', 1)[0]

        for match in self._urls:
            if request.method != match['method']:
                continue

            # TODO(dcramer): we could simplify this by compiling a single
            # regexp on register
            if match['match_querystring']:
                if not re.match(re.escape(match['url']), url):
                    continue
            else:
                if match['url'] != url_without_qs:
                    continue

            return match

        return None

    def _get_response(self, match, request):
        if match is None:
            return None

        if match['side_effect'] is not None:
            if hasattr(match['side_effect'], '__call__'):
                side_effect = match['side_effect']
                return side_effect(Request._from_prepared_request(request))
            elif isinstance(match['side_effect'], Iterator):
                return next(match['side_effect'])
        else:
            return match['response']

    def _on_request(self, request, **kwargs):
        match = self._find_match(request)
        response = self._get_response(match, request)

        # TODO(dcramer): find the correct class for this
        if response is None:
            error_msg = 'Connection refused: {0}'.format(request.url)
            response = ConnectionError(error_msg)

            self._calls.add(request, response)
            raise response

        response = response._as_requests_response(request)

        self._calls.add(request, response)

        return response

    def start(self):
        import mock
        self._patcher = mock.patch('requests.Session.send', self._on_request)
        self._patcher.start()

    def stop(self):
        self._patcher.stop()


# expose default mock namespace
_default_mock = RequestsMock()
__all__ = []
for __attr in (a for a in dir(_default_mock) if not a.startswith('_')):
    __all__.append(__attr)
    globals()[__attr] = getattr(_default_mock, __attr)
