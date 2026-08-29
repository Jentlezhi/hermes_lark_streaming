"""配置读取 — 从 Hermes 主配置解析本插件所需配置项.

设计约束：

1. **所有配置项都有安全默认值**，配置缺失时按保守值加载，绝不因缺项改变行为
2. **类型不可信**：YAML 解析结果是任意结构，每一层取值都做类型校验后再用
3. **短 TTL 缓存而非一次性缓存**：gateway 是长驻进程，一次性缓存意味着改任何
   配置都必须重启。TTL 让配置改完即生效（最长 5 秒），同时避免了「每次读取都
   解析 yaml」——推理流式每秒会读几十次展示开关，那条路径不能带 IO
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import yaml

# 飞书 SDK 根域名。Larksuite（国际版）需切到 open.larksuite.com
DEFAULT_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"

# 卡片元素预算：飞书单卡硬上限 200，留 20 给 footer 与估算误差
DEFAULT_ELEMENT_THRESHOLD = 180

#: turn 空闲多久后由守护强制收卡（秒）。
#:
#: 这里的「空闲」已排除工具执行期间——守护判定时会先看有没有工具还在跑
#: （见 ``_IdleWatcher._sweep``），所以这个值指的是**确实什么都没在跑**的时长，
#: 不需要为一次几分钟的编译或测试预留余量。真正挂死的情况另有
#: :attr:`Config.turn_ttl_sec` 兜底。
DEFAULT_IDLE_FINALIZE_SEC = 90

#: 单次飞书 API 请求的超时（秒）。
#:
#: 取 30 是为了与 lark-oapi 自己的默认值一致（``core/model/config.py`` 里
#: ``timeout: Optional[float] = 30``），配置缺失时行为与不带这个特性时逐字相同。
#:
#: 值得知道的取舍：流式打字机每 100ms 就要发一次请求，而 :class:`FlushScheduler`
#: 对刷新是互斥的——一个请求挂满 30 秒，这期间所有增量都在内存里积压。网络差
#: 的环境可以调大，追求「宁可丢一帧也不要卡住」的可以调小到几秒。
DEFAULT_REQUEST_TIMEOUT_SEC = 30

#: 主配置缓存有效期（秒）。取 5 秒的理由：用户改完 config.yaml 切回飞书发下
#: 一条消息，间隔本身就超过 5 秒，再短没有可感知收益；而每 5 秒一次 stat
#: 的开销可以忽略。刻意不用文件监听——inotify/FSEvents 要引入平台差异与
#: 后台线程，对一个小 yaml 是过度设计
CONFIG_CACHE_TTL_SEC = 5.0


def hermes_home() -> Path:
    """Hermes 主目录.

    优先走 Hermes 官方 API（多 profile 场景下它是唯一真源），
    不可用时回退环境变量，最后回退 ``~/.hermes``。
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    try:
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 env 文件.

    只做最小解析：跳过空行与注释，剥掉可选的 ``export`` 前缀与包裹引号。
    不支持变量插值——那是 shell 的语义，env 文件里出现即视为字面量。
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return values

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


#: env 文件内容缓存，按路径键入。凭据不常变，避免每次取值都读盘
_ENV_FILE_CACHE: dict[str, dict[str, str]] = {}


def _env_file_secret(name: str, home: Path | None = None) -> str:
    """从 Hermes 主目录旁的 ``.env`` 读取凭据.

    **为什么需要这一层**：Hermes gateway 启动时会把 ``.env`` 加载进进程环境，
    所以运行时读 ``os.environ`` 就够了。但 CLI（status / verify / doctor）是
    独立进程，环境里没有这些变量——若不读文件，诊断会把已经配好的凭据
    误报为「缺失」，把用户引向错误的排查方向。
    """
    env_path = (home or hermes_home()) / ".env"
    key = str(env_path)
    cached = _ENV_FILE_CACHE.get(key)
    if cached is None:
        cached = _parse_env_file(env_path) if env_path.exists() else {}
        _ENV_FILE_CACHE[key] = cached
    return cached.get(name, "")


def _get_secret(name: str, home: Path | None = None) -> str:
    """读取凭据.

    优先级：Hermes secret_scope（多 profile 隔离的真源）→ 进程环境变量
    （gateway 启动时已加载 .env）→ 配置目录旁的 .env 文件（CLI 兜底）。

    **注意 secret_scope 是进程级的**：它读的是当前进程所属 profile，不随
    ``home`` 参数变化。因此在 Hermes 进程内用另一个 profile 的 home 构造
    Config，仍可能拿到当前 profile 的凭据。生产路径上两者一致（编排器用
    ``hermes_home()`` 构造），只在跨 profile 探查时需要意识到这点。
    """
    scoped = ""
    try:
        from agent.secret_scope import get_secret  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        try:
            scoped = get_secret(name, "") or ""
        except Exception:
            scoped = ""
    if scoped:
        return scoped

    from_env = os.environ.get(name, "")
    if from_env:
        return from_env

    return _env_file_secret(name, home)


def _as_dict(value: Any) -> dict[str, Any]:
    """把任意值收敛为字典，非字典一律返回空字典.

    这是 9.5「禁类型盲信」在配置层的落点：YAML 里写错缩进就会让
    某个 section 变成 str 或 list，直接 .get() 会抛 AttributeError。
    """
    return value if isinstance(value, dict) else {}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "on", "1"}:
            return True
        if lowered in {"false", "no", "off", "0"}:
            return False
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """转整数并夹取到合法区间；布尔值不视为整数."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    result = int(value)
    if minimum is not None and result < minimum:
        return minimum
    if maximum is not None and result > maximum:
        return maximum
    return result


def _as_str(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


class Config:
    """插件配置，惰性读取 Hermes 主配置.

    构造开销极低（不做 IO），首次访问属性时才读盘。之后走 TTL 缓存：
    最长 :data:`CONFIG_CACHE_TTL_SEC` 秒后重新检查文件指纹，指纹未变则只续期
    不重新解析。因此改配置无需重启 gateway，而热路径也不会为此付出 IO 代价。

    **线程安全**：缓存字段的更新是「先构造完整对象再整体赋值」，GIL 保证单次
    赋值原子，读者永远看到的是某个完整版本，不会读到半更新状态。因此不加锁——
    这条路径在流式回调里被高频调用，加锁的代价大于收益。
    """

    __slots__ = ("_home", "_loaded_at", "_raw", "_stamp")

    def __init__(self, home: Path | None = None) -> None:
        self._home = Path(home) if home is not None else None
        self._raw: dict[str, Any] | None = None
        self._loaded_at = 0.0
        self._stamp: tuple[int, int] = (-1, -1)

    # ── 配置文件读取 ────────────────────────────────────────────────

    def _config_path(self) -> Path:
        return (self._home or hermes_home()) / "config.yaml"

    def _config_stamp(self) -> tuple[int, int]:
        """配置文件指纹：mtime 纳秒 + 字节数.

        比单看 mtime 更难漏检——同一时间戳内的编辑通常伴随大小变化。
        文件不存在时返回 ``(-1, -1)``，与「存在但空」区分开。
        """
        try:
            st = self._config_path().stat()
        except OSError:
            return (-1, -1)
        return (st.st_mtime_ns, st.st_size)

    def _load(self) -> dict[str, Any]:
        """读取主配置（TTL 缓存 + 指纹校验）.

        三级短路，从便宜到贵：TTL 未到期直接返回 → 指纹未变只续期 → 真正读盘。
        用 :func:`time.monotonic` 而非 ``time.time``，避免系统时钟跳变导致
        缓存永不过期或频繁过期。
        """
        now = time.monotonic()
        cached = self._raw
        if cached is not None and now - self._loaded_at < CONFIG_CACHE_TTL_SEC:
            return cached

        stamp = self._config_stamp()
        if cached is not None and stamp == self._stamp:
            self._loaded_at = now
            return cached

        fresh = self._read_disk()
        self._raw = fresh
        self._stamp = stamp
        self._loaded_at = now
        return fresh

    def invalidate(self) -> None:
        """立即作废缓存，下次读取必然重新解析.

        供测试与 ``doctor`` 一类需要「保证读到最新」的场景使用。
        """
        self._raw = None
        self._loaded_at = 0.0
        self._stamp = (-1, -1)

    def _read_disk(self) -> dict[str, Any]:
        path = self._config_path()
        try:
            if not path.exists():
                return {}
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            # 配置损坏不能让插件把 Hermes 拖死，按空配置处理即整体禁用
            return {}
        return _as_dict(loaded)

    def _streaming(self) -> dict[str, Any]:
        return _as_dict(self._load().get("streaming"))

    # ── 总开关与凭据 ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """是否启用流式卡片."""
        return _as_bool(self._streaming().get("enabled"), False)

    def _secret(self, name: str) -> str:
        """按本实例绑定的 profile home 读取凭据."""
        return _get_secret(name, self._home)

    @property
    def env_app_id(self) -> str:
        return self._secret("FEISHU_APP_ID") or self._secret("LARK_APP_ID")

    @property
    def env_app_secret(self) -> str:
        return self._secret("FEISHU_APP_SECRET") or self._secret("LARK_APP_SECRET")

    @property
    def app_id(self) -> str:
        return _as_str(self._platform_cfg().get("app_id"), "")

    @property
    def app_secret(self) -> str:
        return _as_str(self._platform_cfg().get("app_secret"), "")

    @property
    def base_url(self) -> str:
        return _as_str(self._platform_cfg().get("base_url"), DEFAULT_DOMAIN)

    @property
    def has_credentials(self) -> bool:
        return bool((self.app_id or self.env_app_id) and (self.app_secret or self.env_app_secret))

    def _env_base_url(self) -> str:
        """从环境推导 SDK 根域名.

        除了显式的 ``*_BASE_URL``，还识别 Hermes 自己用的 ``FEISHU_DOMAIN``：
        它取 ``lark`` 时表示国际版，对应 open.larksuite.com。
        """
        explicit = self._secret("FEISHU_BASE_URL") or self._secret("LARK_BASE_URL")
        if explicit:
            return explicit
        domain = (self._secret("FEISHU_DOMAIN") or self._secret("LARK_DOMAIN")).strip().lower()
        if domain in ("lark", "larksuite", "global", "international"):
            return LARK_DOMAIN
        if domain.startswith(("http://", "https://")):
            return domain
        return DEFAULT_DOMAIN

    def _platform_cfg(self) -> dict[str, Any]:
        """解析飞书凭据.

        优先级：环境变量 → 顶层 feishu/lark → gateway.platforms.* → platforms.*
        与 Hermes 自身的多种配置写法保持兼容。
        """
        if self.env_app_id and self.env_app_secret:
            return {
                "app_id": self.env_app_id,
                "app_secret": self.env_app_secret,
                "base_url": self._env_base_url(),
            }

        raw = self._load()
        for key in ("feishu", "lark"):
            section = _as_dict(raw.get(key))
            if section.get("app_id"):
                return section

        # Hermes 把平台配置放在 gateway.platforms.<name>.extra 下
        parents: list[dict[str, Any]] = []
        gateway_platforms = _as_dict(_as_dict(raw.get("gateway")).get("platforms"))
        if gateway_platforms:
            parents.append(gateway_platforms)
        top_platforms = _as_dict(raw.get("platforms"))
        if top_platforms:
            parents.append(top_platforms)

        for parent in parents:
            for key in ("feishu", "lark"):
                platform = _as_dict(parent.get(key))
                extra = _as_dict(platform.get("extra"))
                if not extra.get("app_id"):
                    continue
                result = dict(extra)
                if "base_url" not in result and "base_url" in platform:
                    result["base_url"] = platform["base_url"]
                if "base_url" not in result:
                    domain = str(extra.get("domain", platform.get("domain", ""))).lower()
                    if domain == "lark":
                        result["base_url"] = LARK_DOMAIN
                return result
        return {}

    # ── 卡片外观 ──────────────────────────────────────────────────

    @property
    def header_enabled(self) -> bool:
        """卡片顶部状态条.

        默认 **开启**（与参考实现相反）：状态条是「切走后看不出任务是否完成」
        的治理手段之一，见架构设计 6.4。
        """
        return _as_bool(_as_dict(self._streaming().get("header")).get("enabled"), True)

    @property
    def footer_enabled(self) -> bool:
        return _as_bool(_as_dict(self._streaming().get("footer")).get("enabled"), True)

    @property
    def footer_text_size(self) -> str:
        return _as_str(_as_dict(self._streaming().get("footer")).get("text_size"), "notation")

    @property
    def footer_show_label(self) -> bool:
        return _as_bool(_as_dict(self._streaming().get("footer")).get("show_label"), False)

    @property
    def footer_fields(self) -> list[list[str]]:
        """Footer 字段布局（二维数组，每个子数组渲染为一行）."""
        default: list[list[str]] = [["status", "elapsed", "context", "model"]]
        fields = _as_dict(self._streaming().get("footer")).get("fields")
        if not isinstance(fields, list) or not fields:
            return default
        # 一维数组自动包装为二维，容忍用户简写
        if all(isinstance(item, str) for item in fields):
            return [[str(item) for item in fields]]
        rows: list[list[str]] = []
        for row in fields:
            if isinstance(row, list):
                rows.append([str(item) for item in row if isinstance(item, str)])
        return rows or default

    @property
    def body_text_size(self) -> str:
        return _as_str(_as_dict(self._streaming().get("body")).get("text_size"), "normal_v2")

    @property
    def width_mode(self) -> str:
        raw = _as_str(self._streaming().get("width_mode"), "default").lower()
        return raw if raw in {"default", "compact", "fill"} else "default"

    @property
    def panel_expanded(self) -> bool:
        """完成态卡片中推理面板与工具面板是否保持展开."""
        return _as_bool(self._streaming().get("panel_expanded"), False)

    @property
    def icons(self) -> dict[str, str]:
        """卡片符号覆盖表 ``{语义键: 符号}``.

        只做类型过滤，不校验键名——键名的合法性由
        :func:`~hermes_lark_streaming.render.icons.resolve` 负责（它只认已知键，
        未知键静默忽略）。两层职责分开：这里只保证拿到的是 ``str → str``。

        空字符串是合法值，表示「这个位置不要符号」，所以不能顺手用 ``_as_str``
        把空值当缺失处理。
        """
        raw = _as_dict(self._streaming().get("icons"))
        return {
            key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)
        }


    # ── 会话列表状态摘要（治理「切走后看不出是否完成」）────────────

    @property
    def summary_enabled(self) -> bool:
        return _as_bool(_as_dict(self._streaming().get("summary")).get("enabled"), True)

    @property
    def summary_max_chars(self) -> int:
        return _as_int(_as_dict(self._streaming().get("summary")).get("max_chars"), 60, minimum=16, maximum=200)

    # ── 游离消息收纳开关（可逐项关闭以便排障）──────────────────────

    def capture_enabled(self, kind: str) -> bool:
        """某类游离消息是否收纳进卡片.

        kind 取值：notice / review / clarify / approval。
        默认全开；任一项出问题时可单独关闭该项而不影响其他能力。
        """
        capture = _as_dict(self._streaming().get("capture"))
        return _as_bool(capture.get(kind), True)

    def capture_explicit(self, kind: str) -> bool | None:
        """用户是否**显式**配置了该项；``None`` 表示未配置.

        自愈层必须区分「用户没写、用的是默认 True」与「用户明确写了 true」：
        前者允许被学到的降级经验覆盖，后者一切照用户说的办。插件绝不偷改
        用户写死的设定，只在报告里说明「该项持续失败但按你的配置仍在尝试」。

        实现用两次不同默认值的解析对比：值能被解析时两者一致，无法解析时
        各自返回自己的默认值而不相等——这样就不必在此重复一份字面量集合，
        避免与 :func:`_as_bool` 的取值范围产生分歧。
        """
        capture = _as_dict(self._streaming().get("capture"))
        if kind not in capture:
            return None
        raw = capture[kind]
        as_true = _as_bool(raw, True)
        return as_true if as_true == _as_bool(raw, False) else None

    # ── 资源边界 ──────────────────────────────────────────────────

    @property
    def max_turns(self) -> int:
        return _as_int(_as_dict(self._streaming().get("limits")).get("max_turns"), 256, minimum=8, maximum=4096)

    @property
    def turn_ttl_sec(self) -> int:
        return _as_int(
            _as_dict(self._streaming().get("limits")).get("turn_ttl_sec"), 600, minimum=30, maximum=86400
        )

    @property
    def element_threshold(self) -> int:
        return _as_int(
            _as_dict(self._streaming().get("limits")).get("element_threshold"),
            DEFAULT_ELEMENT_THRESHOLD,
            minimum=20,
            maximum=199,
        )

    @property
    def idle_finalize_sec(self) -> int:
        """turn 空闲多久后由守护强制收卡（秒）.

        下限取 15 秒：比守护的扫描间隔（15 秒）更小的阈值没有意义，实际生效
        粒度受扫描间隔限制。默认值的语义见 :data:`DEFAULT_IDLE_FINALIZE_SEC`。
        """
        return _as_int(
            _as_dict(self._streaming().get("limits")).get("idle_finalize_sec"),
            DEFAULT_IDLE_FINALIZE_SEC,
            minimum=15,
            maximum=86400,
        )

    @property
    def bypass_after_failures(self) -> int:
        """连续多少次收纳失败后，织入层进入全局透传（熔断）."""
        return _as_int(
            _as_dict(self._streaming().get("resilience")).get("bypass_after_failures"),
            5,
            minimum=1,
            maximum=1000,
        )

    @property
    def request_timeout_sec(self) -> int:
        """单次飞书 API 请求的超时（秒）.

        下限 3 秒：再小连 TLS 握手加一次往返都未必够，只会把正常请求判成失败。
        默认值与取舍见 :data:`DEFAULT_REQUEST_TIMEOUT_SEC`。
        """
        return _as_int(
            _as_dict(self._streaming().get("resilience")).get("request_timeout_sec"),
            DEFAULT_REQUEST_TIMEOUT_SEC,
            minimum=3,
            maximum=300,
        )

    # ── 自愈层（精准降级 + 经验继承）────────────────────────────────

    def _selfheal(self) -> dict[str, Any]:
        return _as_dict(self._streaming().get("selfheal"))

    @property
    def selfheal_enabled(self) -> bool:
        """是否启用自愈层.

        关闭后本插件行为与 v1.0 完全一致：只有粗粒度全局熔断，不落任何
        状态文件、不做经验继承。自愈层是完全旁路的，关掉不影响任何功能。
        """
        return _as_bool(self._selfheal().get("enabled"), True)

    @property
    def degrade_after_failures(self) -> int:
        """单个能力连续失败多少次后**单独**降级（不影响其余能力）.

        默认 3，比全局熔断阈值（5）更敏感：精准降级的代价远小于全局熔断，
        因此可以更早触发，让问题局限在一类消息上。
        """
        return _as_int(self._selfheal().get("degrade_after_failures"), 3, minimum=1, maximum=100)

    @property
    def selfheal_probe_interval(self) -> int:
        """已降级的能力每被拦截多少次后试探一次恢复."""
        return _as_int(self._selfheal().get("probe_interval"), 20, minimum=1, maximum=10000)

    # ── 多 bot 投递（吸收 HFC 的 BotRegistry 设计）────────────────

    def _plugin_feishu(self) -> dict[str, Any]:
        """本插件在 ``feishu`` / ``lark`` 段下的扩展配置.

        与 :meth:`_platform_cfg` 刻意分开：那个负责兼容 Hermes 的多种凭据
        写法（含 env 与 gateway.platforms 嵌套），这里只读本插件新增的键，
        不参与凭据解析，避免两套逻辑互相干扰。
        """
        raw = self._load()
        for key in ("feishu", "lark"):
            section = _as_dict(raw.get(key))
            if section:
                return section
        return {}

    @property
    def bots(self) -> dict[str, dict[str, str]]:
        """附加 bot 凭据表 ``{bot_id: {app_id, app_secret, base_url}}``.

        未配置时返回空字典，此时全部会话走顶层单套凭据——即默认行为与
        不带这个特性时逐字一致。缺 app_id 或 app_secret 的条目直接丢弃：
        半套凭据构造 client 必然失败，不如当它不存在。
        """
        result: dict[str, dict[str, str]] = {}
        for raw_id, raw_bot in _as_dict(self._plugin_feishu().get("bots")).items():
            bot = _as_dict(raw_bot)
            app_id = _as_str(bot.get("app_id"), "")
            app_secret = _as_str(bot.get("app_secret"), "")
            if not isinstance(raw_id, str) or not app_id or not app_secret:
                continue
            result[raw_id.strip()] = {
                "app_id": app_id,
                "app_secret": app_secret,
                # 未指定则继承顶层域名，避免多 bot 场景下漏配国际版域名
                "base_url": _as_str(bot.get("base_url"), self.base_url),
            }
        return result

    @property
    def chat_bindings(self) -> dict[str, str]:
        """``chat_id -> bot_id``。只保留指向真实存在的 bot 的绑定."""
        known = self.bots
        result: dict[str, str] = {}
        for chat_id, bot_id in _as_dict(self._plugin_feishu().get("chat_bindings")).items():
            if isinstance(chat_id, str) and isinstance(bot_id, str) and bot_id.strip() in known:
                result[chat_id.strip()] = bot_id.strip()
        return result

    @property
    def native_chats(self) -> frozenset[str]:
        """完全不接管的会话.

        命中的会话一张卡片都不建，全部输出走 Hermes 原生路径。用于公共大群
        这类「不希望出现流式卡片」的场景——这是 HFC 的 ``ChatDeliveryPolicy``
        里值得拿过来的一条：接管与否应当是每个会话可选的，而不是全局开关。
        """
        raw = self._plugin_feishu().get("native_chats")
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(item.strip() for item in raw if isinstance(item, str) and item.strip())

    # ── 图片 ──────────────────────────────────────────────────────

    @property
    def image_allow_private_hosts(self) -> bool:
        """是否允许抓取内网 / 环回地址上的图片.

        **默认关闭。** 待上传的图片 URL 来自模型输出的 markdown，属于不可信
        输入——一次 prompt injection（模型读了恶意网页或文件）就能让 gateway 去
        请求任意地址，而它跑在用户自己的机器上、看得到内网。

        被拦下的图片保留原始 markdown 链接，与网络失败同一条降级路径，不影响
        回答的其余内容。确实要用内网图床的场景才需要打开这一项。
        """
        return _as_bool(_as_dict(self._streaming().get("images")).get("allow_private_hosts"), False)

    # ── 订阅额度 ──────────────────────────────────────────────────


    @property
    def usage_enabled(self) -> bool:
        """是否在卡片 footer 展示订阅额度.

        **默认关闭**：查询额度要打服务商的外部 API，属于可见副作用，不该在
        用户没要求时自动发生。开启后仍需把 ``usage`` 写进 ``footer.fields``
        才会显示。
        """
        return _as_bool(_as_dict(self._streaming().get("usage")).get("enabled"), False)

    @property
    def usage_ttl_sec(self) -> int:
        """额度查询结果的缓存时长。额度是慢变量，不必每轮都打 API."""
        return _as_int(_as_dict(self._streaming().get("usage")).get("ttl_sec"), 300, minimum=10, maximum=86400)

    @property
    def hermes_provider(self) -> str:
        """Hermes 当前使用的模型服务商（``model.provider``）.

        用于选择额度查询的实现。Hermes 只为 openai-codex / anthropic /
        openrouter 实现了额度接口，其余服务商查不到数据是正常的。
        """
        return _as_str(_as_dict(self._load().get("model")).get("provider"), "")

    # ── 运行期可变（TTL 缓存，最长 5 秒后生效）────────────────────────

    @property
    def show_reasoning(self) -> bool:
        """是否展示推理过程.

        Hermes 的 ``/reasoning`` 命令会在运行期改写配置文件，因此这一项必须
        跟随磁盘变化。**但不能每次都读盘**：推理流式每秒调用几十次，那条路径
        在 Agent worker 线程上同步执行，一次 yaml 解析就是一次卡顿。
        交给 :meth:`_load` 的 TTL 缓存，改完最长 5 秒生效。
        """
        display = _as_dict(self._load().get("display"))
        feishu = _as_dict(_as_dict(display.get("platforms")).get("feishu"))
        if "show_reasoning" in feishu:
            return _as_bool(feishu["show_reasoning"], False)
        return _as_bool(display.get("show_reasoning"), False)

    @property
    def show_tool_use(self) -> bool:
        """是否展示工具调用面板.

        平台级配置优先于全局；默认 True 保持信息完整。
        """
        display = _as_dict(self._load().get("display"))
        feishu = _as_dict(_as_dict(display.get("platforms")).get("feishu"))
        if "show_tool_use" in feishu:
            return _as_bool(feishu["show_tool_use"], True)
        return _as_bool(display.get("show_tool_use"), True)
