"""DeepSeek 请求重试测试：模拟网络响应，不访问真实 API。"""

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import Mock

import pytest

from music_agent.clients import deepseek


@pytest.fixture
def network(monkeypatch):
    """替换网络请求和等待函数，避免联网及实际等待。"""
    open_mock = Mock()
    sleep_mock = Mock()

    monkeypatch.setattr(deepseek, "urlopen", open_mock)
    monkeypatch.setattr(deepseek.time, "sleep", sleep_mock)

    request = Request("https://api.deepseek.com/chat/completions")
    return request, open_mock, sleep_mock


@pytest.mark.parametrize("status", [429, 503])
def test_temporary_http_error_retries(network, status):
    """限流或临时服务器错误后，应重新请求并返回成功结果。"""
    request, open_mock, sleep_mock = network
    error = HTTPError(request.full_url, status, "临时错误", None, None)
    response = BytesIO(b'{"ok": true}')
    open_mock.side_effect = [error, response]

    result = deepseek.send_request(request)

    assert result == {"ok": True}
    assert open_mock.call_count == 2
    assert sleep_mock.call_count == 1


def test_timeout_stops_after_limit(network):
    """持续超时必须在次数耗尽后报错，不能无限重试。"""
    request, open_mock, sleep_mock = network
    open_mock.side_effect = TimeoutError("模拟超时")

    with pytest.raises(TimeoutError, match="模拟超时"):
        deepseek.send_request(request)

    assert open_mock.call_count == 3
    assert sleep_mock.call_count == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_does_not_retry(network, status):
    """鉴权或权限错误应直接返回，不进行无效重试。"""
    request, open_mock, sleep_mock = network
    open_mock.side_effect = HTTPError(
        request.full_url, status, "鉴权失败", None, None
    )

    with pytest.raises(HTTPError) as caught:
        deepseek.send_request(request)

    assert caught.value.code == status
    assert open_mock.call_count == 1
    sleep_mock.assert_not_called()


def test_invalid_json_does_not_retry(network):
    """响应不是合法 JSON 时，应直接报错。"""
    request, open_mock, sleep_mock = network
    open_mock.return_value = BytesIO(b"not json")

    with pytest.raises(json.JSONDecodeError):
        deepseek.send_request(request)

    assert open_mock.call_count == 1
    sleep_mock.assert_not_called()