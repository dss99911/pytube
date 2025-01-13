"""Implements a simple wrapper around urlopen."""
import http.client
import json
import logging
import re
import socket
from functools import lru_cache
from urllib import parse
from urllib.error import URLError
from urllib.request import Request, urlopen
import requests
from pytube.exceptions import RegexMatchError, MaxRetriesExceeded
from pytube.helpers import regex_search
import pytube.helpers as helper

logger = logging.getLogger(__name__)
default_range_size = 9437184  # 9MB


def _execute_request(
    url,
    method=None,
    headers=None,
    data=None,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    stream=False,
):
    logger.debug(f"url: {url}")

    # 기본 헤더 설정
    base_headers = {"User-Agent": "Mozilla/5.0", "accept-language": "en-US,en"}
    if headers:
        base_headers.update(headers)

    # 데이터 JSON 직렬화
    if data and not isinstance(data, (str, bytes)):
        data = json.dumps(data)

    if not url.lower().startswith("http"):
        raise ValueError("Invalid URL")

    # HTTP 요청
    response = requests.request(method=method, url=url, headers=base_headers, data=data,
                                timeout=timeout, stream=stream, proxies=helper.proxies)

    # 응답 반환
    response.raise_for_status()  # 요청 실패 시 예외 발생
    return response


def get(url, extra_headers=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http GET request.

    :param str url:
        The URL to perform the GET request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    if extra_headers is None:
        extra_headers = {}
    response = _execute_request(url, headers=extra_headers, timeout=timeout)
    return response.text


def post(url, extra_headers=None, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """Send an http POST request.

    :param str url:
        The URL to perform the POST request for.
    :param dict extra_headers:
        Extra headers to add to the request
    :param dict data:
        The data to send on the POST request
    :rtype: str
    :returns:
        UTF-8 encoded string of response
    """
    # could technically be implemented in get,
    # but to avoid confusion implemented like this
    if extra_headers is None:
        extra_headers = {}
    if data is None:
        data = {}
    # required because the youtube servers are strict on content type
    # raises HTTPError [400]: Bad Request otherwise
    extra_headers.update({"Content-Type": "application/json"})
    response = _execute_request(
        url,
        headers=extra_headers,
        data=data,
        timeout=timeout
    )
    return response.text


def seq_stream(
    url,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    max_retries=0
):
    """Read the response in sequence.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = '%s://%s/%s?' % (split_url.scheme, split_url.netloc, split_url.path)

    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys['sq'] = 0
    url = base_url + parse.urlencode(querys)

    segment_data = b''
    for chunk in stream(url, timeout=timeout, max_retries=max_retries):
        yield chunk
        segment_data += chunk

    # We can then parse the header to find the number of segments
    stream_info = segment_data.split(b'\r\n')
    segment_count_pattern = re.compile(b'Segment-Count: (\\d+)')
    for line in stream_info:
        match = segment_count_pattern.search(line)
        if match:
            segment_count = int(match.group(1).decode('utf-8'))

    # We request these segments sequentially to build the file.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys['sq'] = seq_num
        url = base_url + parse.urlencode(querys)

        yield from stream(url, timeout=timeout, max_retries=max_retries)
        seq_num += 1
    return  # pylint: disable=R1711


def stream(
    url,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    max_retries=0,
    chunk_size=8192
):
    """Read the response in chunks.
    :param str url: The URL to perform the GET request for.
    :rtype: Iterable[bytes]
    """
    file_size: int = default_range_size  # fake filesize to start
    downloaded = 0
    while downloaded < file_size:
        stop_pos = min(downloaded + default_range_size, file_size) - 1
        range_header = f"bytes={downloaded}-{stop_pos}"
        tries = 0

        # Attempt to make the request multiple times as necessary.
        while tries <= max_retries:
            try:
                response = _execute_request(
                    url + f"&range={downloaded}-{stop_pos}",
                    method="GET",
                    timeout=timeout,
                    stream=True
                )
                break
            except (requests.Timeout, requests.ConnectionError) as e:
                if tries >= max_retries:
                    raise MaxRetriesExceeded(f"Max retries exceeded: {e}")
                tries += 1
                logger.warning(f"Retrying... {tries}/{max_retries}")
            except requests.RequestException as e:
                logger.error(f"Request failed: {e}")
                raise

        if file_size == default_range_size:
            try:
                resp = _execute_request(
                    url + f"&range={0}-{99999999999}",
                    method="GET",
                    timeout=timeout
                )
                content_range = resp.headers["Content-Length"]
                file_size = int(content_range)
            except (KeyError, IndexError, ValueError) as e:
                logger.error(e)
        # Read the response in chunks and yield them.
        for chunk in response.iter_content(chunk_size=chunk_size):
            if chunk:
                downloaded += len(chunk)
                yield chunk
    return  # pylint: disable=R1711


@lru_cache()
def filesize(url):
    """Fetch size in bytes of file at given URL

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    return int(head(url)["content-length"])


@lru_cache()
def seq_filesize(url):
    """Fetch size in bytes of file at given URL from sequential requests

    :param str url: The URL to get the size of
    :returns: int: size in bytes of remote file
    """
    total_filesize = 0
    # YouTube expects a request sequence number as part of the parameters.
    split_url = parse.urlsplit(url)
    base_url = '%s://%s/%s?' % (split_url.scheme, split_url.netloc, split_url.path)
    querys = dict(parse.parse_qsl(split_url.query))

    # The 0th sequential request provides the file headers, which tell us
    #  information about how the file is segmented.
    querys['sq'] = 0
    url = base_url + parse.urlencode(querys)
    response = _execute_request(
        url, method="GET"
    )

    response_value = response.content
    # The file header must be added to the total filesize
    total_filesize += len(response_value)

    # We can then parse the header to find the number of segments
    segment_count = 0
    stream_info = response_value.split(b'\r\n')
    segment_regex = b'Segment-Count: (\\d+)'
    for line in stream_info:
        # One of the lines should contain the segment count, but we don't know
        #  which, so we need to iterate through the lines to find it
        try:
            segment_count = int(regex_search(segment_regex, line, 1))
        except RegexMatchError:
            pass

    if segment_count == 0:
        raise RegexMatchError('seq_filesize', segment_regex)

    # We make HEAD requests to the segments sequentially to find the total filesize.
    seq_num = 1
    while seq_num <= segment_count:
        # Create sequential request URL
        querys['sq'] = seq_num
        url = base_url + parse.urlencode(querys)

        total_filesize += int(head(url)['content-length'])
        seq_num += 1
    return total_filesize


def head(url):
    """Fetch headers returned http GET request.

    :param str url:
        The URL to perform the GET request for.
    :rtype: dict
    :returns:
        dictionary of lowercase headers
    """
    response_headers = _execute_request(url, method="HEAD").headers
    return {k.lower(): v for k, v in response_headers.items()}
