"""飞书 API 客户端 — 基于 lark-oapi 官方 SDK.

**为什么用官方 SDK 而不是自己拼 HTTP**：CardKit v2.0 的流式接口有严格的
sequence 校验与内容编码要求，SDK 封装了 token 刷新、重试、签名与序列化。
自己拼 HTTP 需要重新实现这些，且 CardKit 接口演进时要跟着改。

**关键方法是 :meth:`stream_element`**：它对应 CardKit 的单元素增量更新，
是打字机效果的传输基础。整卡替换（:meth:`update_card`）只在终态使用。
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# SDK 缺失时不能让整个包 import 失败：诊断命令（status / verify / doctor）的
# 全部价值就在于环境不全时还能跑完并说清缺什么。因此这里容错导入，
# 真正需要 SDK 的时机（构造 FeishuClient）再明确报错。
try:
    import lark_oapi as lark
    from lark_oapi.api.cardkit.v1 import (
        BatchUpdateCardRequest,
        BatchUpdateCardRequestBody,
        Card,
        ContentCardElementRequest,
        ContentCardElementRequestBody,
        CreateCardRequest,
        CreateCardRequestBody,
        SettingsCardRequest,
        SettingsCardRequestBody,
        UpdateCardRequest,
        UpdateCardRequestBody,
    )
    from lark_oapi.api.im.v1 import (
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    SDK_AVAILABLE = True
    SDK_IMPORT_ERROR = ""
except ImportError as _sdk_error:  # pragma: no cover - 取决于运行环境
    SDK_AVAILABLE = False
    SDK_IMPORT_ERROR = str(_sdk_error)

from ..config import DEFAULT_DOMAIN, DEFAULT_REQUEST_TIMEOUT_SEC
from ..observability import METRICS, logger, redact
from .resilience import RETRY_DELAYS, TRANSIENT_CODES, FeishuAPIError

_OPEN_APIS_SUFFIX = "/open-apis"
#: 远程图片下载上限，防止超大图拖垮进程
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_IMAGE_TIMEOUT_SEC = 15


@dataclass(frozen=True, slots=True)
class ClientConfig:
    app_id: str
    app_secret: str
    base_url: str = DEFAULT_DOMAIN
    #: 单次请求超时（秒）。SDK 把它同时用在同步（requests）与异步（httpx）两条
    #: 路径上，所以本类的全部方法都受它约束，不需要逐个方法单独设
    timeout_sec: float = float(DEFAULT_REQUEST_TIMEOUT_SEC)

    def __post_init__(self) -> None:
        if not self.app_id.strip():
            raise ValueError("app_id 不能为空")
        if not self.app_secret.strip():
            raise ValueError("app_secret 不能为空")


class FeishuClient:
    """飞书 API 封装.

    SDK 自动管理 tenant_access_token 的获取与刷新，本类不持有任何凭据明文
    之外的状态，可安全地被多个 turn 共享。
    """

    __slots__ = ("_client", "config")

    def __init__(self, config: ClientConfig) -> None:
        if not SDK_AVAILABLE:
            raise RuntimeError(
                f"飞书 SDK 不可用（{SDK_IMPORT_ERROR}）。请在 Hermes 的 venv 中安装本插件："
                "<hermes-python> -m pip install -e ."
            )
        self.config = config
        domain = config.base_url.strip().rstrip("/").removesuffix(_OPEN_APIS_SUFFIX) or DEFAULT_DOMAIN
        self._client = (
            lark.Client.builder()
            .app_id(config.app_id)
            .app_secret(config.app_secret)
            .domain(domain)
            .timeout(config.timeout_sec)
            .build()
        )

    # ── 内部工具 ──────────────────────────────────────────────────

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _check(response: Any, operation: str) -> None:
        if response.success():
            return
        code = int(getattr(response, "code", 0) or 0)
        message = str(getattr(response, "msg", "") or "")
        raise FeishuAPIError(redact(f"{operation} 失败: code={code}, msg={message}"), code, operation)

    async def _call(self, operation: str, invoke: Any) -> Any:
        """执行 SDK 调用并对瞬时错误退避重试.

        只重试 TRANSIENT_CODES：其余错误重试无意义，快速失败让上层走降级。
        """
        attempts = len(RETRY_DELAYS) + 1
        last_error: FeishuAPIError | None = None

        for attempt in range(attempts):
            try:
                response = await invoke()
            except FeishuAPIError:
                raise
            except Exception as exc:
                # SDK 层异常（网络、序列化）统一包装，交由 resilience 分类
                raise FeishuAPIError(redact(f"{operation} 异常: {exc}"), 0, operation) from exc

            try:
                self._check(response, operation)
                METRICS.incr(f"api.{operation}.ok")
                return response
            except FeishuAPIError as exc:
                last_error = exc
                if exc.code not in TRANSIENT_CODES or attempt >= attempts - 1:
                    METRICS.incr(f"api.{operation}.error")
                    raise
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "%s 遇到瞬时错误 code=%s，%.2fs 后重试（第 %d/%d 次）",
                    operation,
                    exc.code,
                    delay,
                    attempt + 2,
                    attempts,
                )
                METRICS.incr(f"api.{operation}.retry")
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    # ── CardKit ───────────────────────────────────────────────────

    async def create_card(self, card: dict[str, Any]) -> str:
        """创建 CardKit 卡片实体，返回 card_id."""
        request = (
            CreateCardRequest.builder()
            .request_body(CreateCardRequestBody.builder().type("card_json").data(self._dumps(card)).build())
            .build()
        )
        response = await self._call("create_card", lambda: self._client.cardkit.v1.card.acreate(request))
        card_id = getattr(getattr(response, "data", None), "card_id", None)
        if not card_id:
            raise FeishuAPIError("create_card 返回缺少 card_id", 0, "create_card")
        return str(card_id)

    async def stream_element(
        self,
        card_id: str,
        element_id: str,
        content: str,
        *,
        sequence: int,
    ) -> None:
        """流式更新单个元素的文本 —— 打字机效果的核心接口.

        只传该元素的完整文本（非增量 diff），飞书据此与上次内容比对后
        由客户端逐字播放差异部分。sequence 必须单调递增，否则服务端丢弃。
        """
        request = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(ContentCardElementRequestBody.builder().content(content).sequence(sequence).build())
            .build()
        )
        # SDK 未提供该接口的异步版本，放到线程池避免阻塞事件循环
        await self._call(
            "stream_element",
            lambda: asyncio.to_thread(self._client.cardkit.v1.card_element.content, request),
        )

    async def batch_update(
        self,
        card_id: str,
        actions: list[dict[str, Any]],
        *,
        sequence: int,
    ) -> None:
        """批量结构性更新（新增元素、局部更新元素）."""
        request = (
            BatchUpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                BatchUpdateCardRequestBody.builder().sequence(sequence).actions(self._dumps(actions)).build()
            )
            .build()
        )
        await self._call("batch_update", lambda: self._client.cardkit.v1.card.abatch_update(request))

    async def update_card(self, card_id: str, card: dict[str, Any], *, sequence: int) -> None:
        """全量替换卡片内容 — 只在终态与封存时使用."""
        request = (
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(Card.builder().type("card_json").data(self._dumps(card)).build())
                .sequence(sequence)
                .build()
            )
            .build()
        )
        await self._call("update_card", lambda: self._client.cardkit.v1.card.aupdate(request))

    async def close_streaming(self, card_id: str, *, sequence: int) -> None:
        """关闭流式模式，卡片进入静态状态."""
        request = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(self._dumps({"streaming_mode": False}))
                .sequence(sequence)
                .build()
            )
            .build()
        )
        await self._call("close_streaming", lambda: self._client.cardkit.v1.card.asettings(request))

    # ── 消息 ──────────────────────────────────────────────────────

    async def reply_with_card(self, message_id: str, card_id: str) -> str:
        """以卡片实体回复某条消息，返回新消息 id."""
        payload = {"type": "card", "data": {"card_id": card_id}}
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(self._dumps(payload))
                .uuid(uuid.uuid4().hex)
                .build()
            )
            .build()
        )
        response = await self._call("reply_with_card", lambda: self._client.im.v1.message.areply(request))
        return self._extract_message_id(response, "reply_with_card")

    async def send_card(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        reply_to_message_id: str | None = None,
    ) -> str:
        """发送独立卡片（cron / background 用），返回消息 id."""
        content = self._dumps(card)
        request_uuid = uuid.uuid4().hex

        if reply_to_message_id:
            request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(content)
                    .uuid(request_uuid)
                    .build()
                )
                .build()
            )
            response = await self._call("send_card", lambda: self._client.im.v1.message.areply(request))
        else:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type("interactive")
                    .content(content)
                    .uuid(request_uuid)
                    .build()
                )
                .build()
            )
            response = await self._call("send_card", lambda: self._client.im.v1.message.acreate(request))

        return self._extract_message_id(response, "send_card")

    @staticmethod
    def _extract_message_id(response: Any, operation: str) -> str:
        message_id = getattr(getattr(response, "data", None), "message_id", None)
        if not message_id:
            raise FeishuAPIError(f"{operation} 返回缺少 message_id", 0, operation)
        return str(message_id)

    # ── 图片 ──────────────────────────────────────────────────────

    async def upload_image(self, image_url: str) -> str | None:
        """下载远程图片并上传飞书，返回 img_key.

        失败返回 None 而非抛错：单张图片失败不应影响整条回答的展示，
        调用方保留原始 markdown 链接即可。
        """
        try:
            data = await asyncio.to_thread(self._download, image_url)
        except Exception:
            logger.debug("图片下载失败: %s", image_url, exc_info=True)
            return None
        if not data:
            return None

        try:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder().image_type("message").image(io.BytesIO(data)).build()
                )
                .build()
            )
            response = await self._client.im.v1.image.acreate(request)
            if response.success() and getattr(response, "data", None):
                image_key = getattr(response.data, "image_key", None)
                if image_key:
                    METRICS.incr("api.upload_image.ok")
                    return str(image_key)
        except Exception:
            logger.debug("图片上传失败: %s", image_url, exc_info=True)
        METRICS.incr("api.upload_image.error")
        return None

    @staticmethod
    def _download(url: str) -> bytes | None:
        if not url.lower().startswith(("http://", "https://")):
            return None
        try:
            request = Request(url, headers={"User-Agent": "hermes-lark-streaming/1.0"})
            with urlopen(request, timeout=_IMAGE_TIMEOUT_SEC) as response:  # noqa: S310 - 已限定 http(s)
                if getattr(response, "status", 200) != 200:
                    return None
                data = response.read(_MAX_IMAGE_BYTES + 1)
            if not data or len(data) > _MAX_IMAGE_BYTES:
                return None
            return bytes(data)
        except (URLError, OSError, ValueError):
            return None
