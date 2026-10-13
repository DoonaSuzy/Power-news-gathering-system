"""配图下载与上传：`pending` → RustFS 的 `downloaded`。

## 为什么是独立模块而非塞进 spider

spider 只**收集 URL**（见 `newsagg/images.py`）。真正下载图片是
**重 IO 操作**（每张图几百 KB、要重试、可能超时），塞进爬取循环会：
- 拖慢文章入库（用户看到的就是「采集变慢了」）
- 一张图卡住会阻塞整批任务

所以拆成**离线批处理**：`pending` 队列由独立入口消费
（`python -m newsagg fetch-images`），可随时中断、可限速、可重跑。

## 两层去重

| 层次 | 机制 | 效果 |
| --- | --- | --- |
| 篇内 | `uk_img_article_url` | 同文重复引用只存一条 |
| **全站** | 上传前查 `url_hash` 是否已有 `downloaded` | **同一张通稿配图只下载一次** |

全站去重是**省钱的关键**：一条通稿被 10 家媒体转载时，
URL 不同（各自的图片服务器）但内容相同 —— 不过那种情况 URL 也不同，
所以**这里按 `object_key` 去重**（object_key 由 url_hash 派生）。

## 失败不重试无限次

`status='failed'` 的记录带 `error` 原因，**不会自动重试**
（避免对着已失效的图片反复打）。需要重试时由运维显式重置为 pending。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

__all__ = ["ImageFetchResult", "ImageStore", "fetcher_for"]

log = logging.getLogger("newsagg.images")

#: 判定为图片的 content-type 前缀
_IMAGE_CT = ("image/",)

#: 单张图最大字节（超过视为异常/占位大图，跳过）
MAX_BYTES = 8 * 1024 * 1024

#: 扩展名 → content-type 兜底映射（部分站点不返回 content-type）
_EXT_CT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif",
}


@dataclass(slots=True)
class ImageFetchResult:
    """单张图的处理结果。

    Attributes:
        image_id: `article_images.id`。
        status: `downloaded` / `failed` / `skipped`。
        object_key: 上传成功时的对象键。
        byte_size: 字节数。
        error: 失败原因（截断到 255 内，适配列宽）。
    """

    image_id: int
    status: str
    object_key: str = ""
    byte_size: int = 0
    error: str = ""


def object_key_for(url_hash: str, url: str, *, prefix: str = "articles") -> str:
    """由 url_hash 派生的对象键（**内容寻址**）。

    形如 ``articles/ab/cd/abcdef...jpg`` —— 打散目录避免单目录文件过多
    （S3 无目录概念，但列表操作按前缀扫描时，打散能显著降低单前缀的条目数）。

    Args:
        url_hash: 40 位 URL 指纹。
        url: 原始地址（用于取扩展名）。
        prefix: 顶层前缀。

    Returns:
        对象键，如 ``articles/ab/cd/abcd....jpg``。
    """
    tail = url.split("?")[0].rsplit(".", 1)
    ext = f".{tail[1].lower()[:5]}" if len(tail) == 2 and tail[1].isalnum() else ".jpg"
    if ext not in _EXT_CT:
        ext = ".jpg"
    return f"{prefix}/{url_hash[:2]}/{url_hash[2:4]}/{url_hash}{ext}"


#: 判定为「正文配图」的最小边长（像素）。
#:
#: 为什么要按尺寸过滤：实测中国电力新闻网一篇文章提取到的 12 张「配图」里，
#: 放大镜图标 19×19、手机图标 32×32、头像 20×20、二维码若干 ——
#: **没有一张是真正的正文图**。路径词表拦不住它们（路径里没有 logo/icon/qr 字样），
#: 但**尺寸是硬特征**：正文配图不可能只有 19 像素宽。
#:
#: 取 100 的依据：实测正文配图多为 250×250 以上；而图标类无一超过 40px。
MIN_IMAGE_PX = 100


def image_dimensions(data: bytes) -> tuple[int, int]:
    """从图片字节流解析宽高（**零依赖** —— 手解文件头）。

    为什么不用 Pillow：为一个「读两个整数」的需求引入图像库不划算
    （镜像体积、构建时间、CVE 面都增加）。四种主流格式的头部足够简单。

    Args:
        data: 图片原始字节。

    Returns:
        ``(宽, 高)``；无法识别时返回 ``(0, 0)``。

    Note:
        只支持 PNG / JPEG / GIF / WebP —— 覆盖本项目实测的全部图片来源。
        其他格式返回 (0,0)，调用方应按「未知尺寸」处理（**不**因此丢弃）。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        # IHDR 紧跟 8 字节签名 + 4 字节长度 + 4 字节类型，宽高各 4 字节大端
        return (int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"))

    if data[:2] == b"\xff\xd8":                       # JPEG SOI
        i = 2
        n = len(data)
        while i < n - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # SOF0..SOF3 段里带尺寸（其余 SOF 是差分/算术编码，少见）
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2                                # 无长度字段的标记
                continue
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg <= 0:
                break
            i += 2 + seg
        return 0, 0

    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"))

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8 ":     # 无损帧头后 2 字节各含 14 位尺寸
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
        if kind == b"VP8L":     # 紧凑位打包：14 位宽 + 14 位高
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if kind == b"VP8X":     # 扩展格式：24 位宽高（减一）
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
    return 0, 0


def fetcher_for(settings: Any) -> Any:
    """按配置构造 HTTP 下载器（httpx，统一 trust_env 策略）。

    Args:
        settings: 项目 Settings。

    Returns:
        httpx.Client 实例。

    Raises:
        RuntimeError: httpx 不可用。
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 httpx 依赖") from exc

    # trust_env=False：不走系统/沙箱代理（沙箱代理会劫持 TLS，
    # 这与 netpolicy 里对爬虫的处理一致）
    return httpx.Client(
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": getattr(settings, "user_agent", "powernews/0.1")},
        trust_env=False,
    )


class ImageStore:
    """图片下载 → RustFS 上传。

    Args:
        settings: 项目 Settings（读 S3 配置）。
        repo: Repository（读写 `article_images`）。
        db: Database（开连接）。
    """

    def __init__(self, settings: Any, repo: Any, db: Any) -> None:
        self.settings = settings
        self.repo = repo
        self.db = db
        self._s3: Any = None

    # ------------------------------------------------------------------
    def _client(self) -> Any:
        """惰性构造 S3 客户端（boto3）。

        Raises:
            RuntimeError: 缺 boto3，或未配置 S3 凭证。
        """
        if self._s3 is not None:
            return self._s3
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺 boto3（图片上传 RustFS 需要）：\n"
                "    uv add boto3   # 或 pip install boto3"
            ) from exc

        endpoint = getattr(self.settings, "s3_endpoint", "")
        key = getattr(self.settings, "s3_access_key", "")
        secret = getattr(self.settings, "s3_secret_key", "")
        if not (endpoint and key and secret):
            raise RuntimeError("未配置 S3 凭证（POWERNEWS_S3_ENDPOINT/ACCESS_KEY/SECRET_KEY）")

        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            region_name="us-east-1",   # RustFS 不校验区域，但 boto3 要求非空
        )
        return self._s3

    @property
    def bucket(self) -> str:
        return getattr(self.settings, "s3_bucket", "powernews-images")

    def ensure_bucket(self) -> None:
        """确保桶存在（幂等）。"""
        s3 = self._client()
        try:
            s3.head_bucket(Bucket=self.bucket)
        except Exception:  # noqa: BLE001
            from botocore.exceptions import ClientError

            try:
                s3.create_bucket(Bucket=self.bucket)
                log.info("已创建桶 %s", self.bucket)
            except ClientError as exc:
                # 并发创建时可能已被别人建好，不算错误
                if "BucketAlreadyOwnedByYou" not in str(exc) and "BucketAlreadyExists" not in str(exc):
                    raise

    # ------------------------------------------------------------------
    def fetch_one(self, *, image_id: int, url: str, url_hash: str,
                  client: Any | None = None) -> ImageFetchResult:
        """下载单张图并上传 RustFS。

        Args:
            image_id: `article_images.id`。
            url: 图片地址。
            url_hash: URL 指纹。
            client: 复用的 httpx.Client（批量时传，省连接开销）。

        Returns:
            :class:`ImageFetchResult`。

        Note:
            **不抛异常** —— 任何失败都转成 `status='failed'` + `error`，
            因为调用方（批处理）要继续处理下一张。
        """
        own_client = client is None
        cli = client or fetcher_for(self.settings)
        try:
            resp = cli.get(url)
            if resp.status_code != 200:
                return ImageFetchResult(image_id, "failed",
                                        error=f"http_{resp.status_code}")

            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
            if ctype and not ctype.startswith(_IMAGE_CT):
                # 不是图片（站点返回了 HTML 错误页）
                return ImageFetchResult(image_id, "failed",
                                        error=f"not_image:{ctype[:40]}")

            data = resp.content
            if not data:
                return ImageFetchResult(image_id, "skipped", error="empty_body")
            if len(data) > MAX_BYTES:
                return ImageFetchResult(image_id, "skipped",
                                        error=f"too_large:{len(data)}")
            if not ctype:
                ext = url.split("?")[0].rsplit(".", 1)
                ctype = _EXT_CT.get(f".{ext[1].lower()}" if len(ext) == 2 else "", "image/jpeg")

            # 尺寸过滤：**这是拦图标/二维码最有效的一道**。
            # 路径词表拦不住 19×19 的放大镜图标（路径里没有 logo/icon 字样），
            # 但正文配图不可能只有 19 像素宽。
            w, h = image_dimensions(data)
            if w and h and (w < MIN_IMAGE_PX or h < MIN_IMAGE_PX):
                return ImageFetchResult(
                    image_id, "skipped", error=f"too_small:{w}x{h}",
                    byte_size=len(data), content_type=ctype,
                    width=w, height=h,
                )

            key = object_key_for(url_hash, url)
            self._client().put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=ctype,
            )
            return ImageFetchResult(image_id, "downloaded", object_key=key,
                                    byte_size=len(data), content_type=ctype,
                                    width=w, height=h)
        except Exception as exc:  # noqa: BLE001
            return ImageFetchResult(image_id, "failed", error=str(exc)[:255])
        finally:
            if own_client:
                cli.close()

    def process_pending(self, *, limit: int = 200) -> dict[str, int]:
        """处理一批 `pending` 图片。

        Args:
            limit: 单批最多处理几张。

        Returns:
            ``{"processed": n, "downloaded": n, "failed": n, "skipped": n}``。
        """
        self.ensure_bucket()
        rows = self.repo.list_pending_images(self.db, limit=limit)
        stats = {"processed": 0, "downloaded": 0, "failed": 0, "skipped": 0}
        if not rows:
            return stats

        conn = self.db.connect()
        try:
            with fetcher_for(self.settings) as client:
                for r in rows:
                    res = self.fetch_one(
                        image_id=r["id"], url=r["url"],
                        url_hash=r["url_hash"], client=client,
                    )
                    self.repo.mark_image_fetched(
                        conn, image_id=res.image_id, status=res.status,
                        object_key=res.object_key, byte_size=res.byte_size,
                        error=res.error, content_type=res.content_type,
                        width=res.width, height=res.height,
                    )
                    stats["processed"] += 1
                    stats[res.status] = stats.get(res.status, 0) + 1
            conn.commit()
        finally:
            conn.close()
        return stats
