"""DeepSeek 接口客户端，负责请求模型并解析 JSON 输出。"""

import json
import hashlib
import os
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
import sys
import time
from urllib.error import HTTPError, URLError

def request_json(system_prompt, user_prompt, *, on_response=None):
    """调用 DeepSeek，返回模型生成的 JSON 对象。
    Args:
        system_prompt: 系统提示词，需要明确要求输出 JSON。
        user_prompt: 用户输入文本。
        on_response: 可选回调，接收调用记录，由调用方负责保存。
    Returns:
        模型生成的字典。具体业务字段由调用方校验。
    Raises:
        ValueError: 密钥缺失、响应异常或输出不是 JSON 对象。
        OSError: 网络请求失败或超时。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("请先设置环境变量 DEEPSEEK_API_KEY")
    if on_response is not None and not callable(on_response):
        raise ValueError("on_response 必须是可调用对象")

    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 8192,
    }

    request = Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    started_at = datetime.now(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%d %H:%M:%S（北京时间）"
    )
    started = time.monotonic()

    def record_response(status, result=None, content=None, finish_reason=None, error=None):
        """记录已发送参数与响应摘要，不记录请求头、完整提示词或异常正文。"""
        if on_response is None:
            return
        raw_content = content if isinstance(content, str) else ""
        envelope = result if isinstance(result, dict) else {}
        record = {
            "status": status,
            "started_at": started_at,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "requested_model": payload["model"],
            "parameters": {
                key: value for key, value in payload.items()
                if key not in {"model", "messages"}
            },
            "response_id": envelope.get("id"),
            "returned_model": envelope.get("model"),
            "finish_reason": finish_reason,
            "usage": envelope.get("usage", {}),
            "response_summary": {
                "characters": len(raw_content),
                "preview": raw_content[:300],
                "sha256": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
            },
        }
        if error is not None:
            # 不保存异常正文，避免网络异常携带敏感请求信息。
            record["error_type"] = type(error).__name__
            if isinstance(error, HTTPError):
                record["http_status"] = error.code
        on_response(record)

    try:
        result = send_request(request)
    except (OSError, ValueError) as error:
        record_response("request_failed", error=error)
        raise

    # 先检查响应结构，避免直接索引异常数据。
    try:
        choice = result["choices"][0]
        finish_reason = choice["finish_reason"]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        record_response("invalid_response", result=result, error=error)
        raise ValueError("DeepSeek 响应结构不合法") from error

    # 先记录接口响应，随后再校验；收到响应不等于产物通过校验。
    record_response("response_received", result, content, finish_reason)

    # 截断或空响应不能交给后续业务处理。
    if finish_reason != "stop":
        raise ValueError(
            f"DeepSeek 响应未正常结束：finish_reason={finish_reason}，"
            f"usage={result.get('usage', {})}"
        )
    if not isinstance(content, str) or not content.strip():
        raise ValueError("DeepSeek 返回了空内容")

    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("DeepSeek 输出必须是 JSON 对象")

    return data

def send_request(request, max_attempts=3):
    """发送请求，仅对部分临时网络错误进行有限重试。

    Args:
        request: 已构造的 urllib Request 对象。
        max_attempts: 总尝试次数，包含第一次请求。
    Returns:
        接口响应解析后的 JSON 数据。
    Raises:
        ValueError: 尝试次数不合法或响应不是有效 JSON。
        HTTPError: 不可重试的 HTTP 错误，或重试次数耗尽。
        URLError: 网络错误且重试次数耗尽。
        TimeoutError: 请求超时且重试次数耗尽。
        ConnectionError: 连接异常且重试次数耗尽。
    """
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("总尝试次数必须是正整数")

    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(max_attempts):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)

        except HTTPError as error:
            # HTTPError 也是 URLError，必须先单独处理。
            status = error.code
            error.close()
            if (
                status not in retryable_statuses
                or attempt == max_attempts - 1
            ):
                raise

        except (URLError, TimeoutError, ConnectionError):
            if attempt == max_attempts - 1:
                raise

        # 默认最多请求三次，两次重试分别等待 1 秒、2 秒。
        delay = 2 ** attempt
        print(
            f"DeepSeek 请求暂时失败，{delay} 秒后重试"
            f"（第 {attempt + 2}/{max_attempts} 次请求）",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
