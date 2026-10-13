"""正文配图提取的回归测试。

重点钉住**过滤规则** —— 抓多了会把 RustFS 塞满垃圾图（logo/二维码/埋点图），
抓少了则丢正文配图。两者都要防。
"""

from __future__ import annotations

from newsagg.images import (
    ImageRef,
    extract_image_urls,
    is_probably_noise_url,
    url_hash40,
)


class TestUrlHash:
    def test_is_40_hex(self):
        h = url_hash40("http://a.cn/x.jpg")
        assert len(h) == 40
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic(self):
        assert url_hash40("http://a.cn/x.jpg") == url_hash40("http://a.cn/x.jpg")

    def test_differs_by_url(self):
        assert url_hash40("http://a.cn/1.jpg") != url_hash40("http://a.cn/2.jpg")


class TestNoiseFilter:
    def test_common_noise_words(self):
        for u in ("http://a.cn/logo.png", "http://a.cn/icon_share.gif",
                  "http://a.cn/qrcode.jpg", "http://a.cn/avatar/1.png",
                  "http://a.cn/loading.gif", "http://a.cn/blank.gif"):
            assert is_probably_noise_url(u), u

    def test_case_insensitive(self):
        assert is_probably_noise_url("http://a.cn/LOGO.PNG")

    def test_normal_image_not_noise(self):
        for u in ("http://a.cn/news/2026/10/pic1.jpg",
                  "http://a.cn/upload/20261010.jpeg"):
            assert not is_probably_noise_url(u), u


class TestExtraction:
    def test_basic_extraction(self):
        html = '<img src="/upload/a.jpg"><img src="/upload/b.png">'
        refs = extract_image_urls(html, "http://a.cn/news/1.html")
        assert [r.url for r in refs] == [
            "http://a.cn/upload/a.jpg", "http://a.cn/upload/b.png",
        ]

    def test_relative_to_absolute(self):
        refs = extract_image_urls('<img src="pic.jpg">', "http://a.cn/2026/10/x.html")
        assert refs[0].url == "http://a.cn/2026/10/pic.jpg"

    def test_protocol_relative(self):
        refs = extract_image_urls('<img src="//cdn.cn/a.jpg">', "https://a.cn/x.html")
        assert refs[0].url == "https://cdn.cn/a.jpg"

    def test_lazy_load_attributes(self):
        """懒加载用 data-src / data-original，src 往往是占位图。"""
        for attr in ("data-src", "data-original", "data-lazy-src"):
            html = f'<img {attr}="/u/real.jpg" src="/spacer.gif">'
            refs = extract_image_urls(html, "http://a.cn/")
            assert any(r.url == "http://a.cn/u/real.jpg" for r in refs), attr

    def test_skips_base64(self):
        """内联 base64 不是外链图 —— 体积大且无法跨文章复用。"""
        html = '<img src="data:image/png;base64,iVBORw0KGgo="><img src="/u/a.jpg">'
        refs = extract_image_urls(html, "http://a.cn/")
        assert [r.url for r in refs] == ["http://a.cn/u/a.jpg"]

    def test_skips_noise(self):
        html = ('<img src="/logo.png"><img src="/u/real.jpg">'
                '<img src="/qrcode.jpg">')
        refs = extract_image_urls(html, "http://a.cn/")
        assert [r.url for r in refs] == ["http://a.cn/u/real.jpg"]

    def test_dedup_same_url(self):
        """正文重复引用同一张图 → 只留一条。"""
        html = '<img src="/u/a.jpg">' + '<img src="/u/a.jpg">' * 3
        refs = extract_image_urls(html, "http://a.cn/")
        assert len(refs) == 1

    def test_fragment_ignored_in_dedup(self):
        """同图不同锚点视为同一张。"""
        html = '<img src="/u/a.jpg#x"><img src="/u/a.jpg#y">'
        refs = extract_image_urls(html, "http://a.cn/")
        assert len(refs) == 1

    def test_ordinal_preserves_order(self):
        html = ('<img src="/u/1.jpg"><img src="/u/2.jpg"><img src="/u/3.jpg">')
        refs = extract_image_urls(html, "http://a.cn/")
        assert [r.ordinal for r in refs] == [0, 1, 2]

    def test_max_images_cap(self):
        html = "".join(f'<img src="/u/{i}.jpg">' for i in range(100))
        refs = extract_image_urls(html, "http://a.cn/", max_images=5)
        assert len(refs) == 5

    def test_alt_text_captured(self):
        refs = extract_image_urls('<img src="/u/a.jpg" alt="施工现场">', "http://a.cn/")
        assert refs[0].alt_text == "施工现场"

    def test_dimensions_captured(self):
        refs = extract_image_urls('<img src="/u/a.jpg" width="800" height="600">', "http://a.cn/")
        assert (refs[0].width, refs[0].height) == (800, 600)

    def test_tiny_declared_size_is_dropped(self):
        """1x1 / 2x2 是埋点图。"""
        html = ('<img src="/u/pixel.gif" width="1" height="1">'
                '<img src="/u/real.jpg" width="800" height="600">')
        refs = extract_image_urls(html, "http://a.cn/")
        assert [r.url for r in refs] == ["http://a.cn/u/real.jpg"]

    def test_empty_html(self):
        assert extract_image_urls("", "http://a.cn/") == []

    def test_no_img_tag(self):
        assert extract_image_urls("<p>纯文字新闻</p>", "http://a.cn/") == []

    def test_img_without_src(self):
        assert extract_image_urls("<img alt='x'>", "http://a.cn/") == []

    def test_returns_immutable_dataclass(self):
        refs = extract_image_urls('<img src="/u/a.jpg">', "http://a.cn/")
        assert isinstance(refs[0], ImageRef)
        try:
            refs[0].url = "http://x"  # type: ignore[misc]
            raise AssertionError("应不可变")
        except (AttributeError, TypeError):
            pass
