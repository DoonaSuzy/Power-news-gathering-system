"""SPA 静态回退的边界：**不能把不存在的接口吞成首页**。

事故背景：生产上 `GET /api/v1/channels`（当时该端点还不存在）返回
`200 text/html` + 前端首页，巡检脚本据此判定「端点存在」；
前端调错接口时也只拿到 HTML，JSON 解析失败却看不出是 404。
"""

from __future__ import annotations

import pytest
from starlette.exceptions import HTTPException as StarletteHTTPException

from newsplatform.main import _is_reserved_path, _SPAStaticFiles


class TestReservedPath:
    @pytest.mark.parametrize(
        "path",
        ["api/v1/channels", "api", "api/", "metrics", "docs", "redoc", "openapi.json"],
    )
    def test_backend_paths_are_reserved(self, path):
        assert _is_reserved_path(path) is True

    @pytest.mark.parametrize("path", ["", "article/123", "apix/1", "apiary"])
    def test_frontend_paths_are_not_reserved(self, path):
        assert _is_reserved_path(path) is False


class TestFallback:
    def _files(self, tmp_path):
        (tmp_path / "index.html").write_text("<html>spa</html>", encoding="utf-8")
        return _SPAStaticFiles(directory=str(tmp_path), html=True)

    async def test_unknown_api_path_raises_404(self, tmp_path):
        files = self._files(tmp_path)
        scope = {"type": "http", "method": "GET", "headers": []}
        with pytest.raises(StarletteHTTPException) as exc:
            await files.get_response("api/v1/not-exist", scope)
        assert exc.value.status_code == 404

    async def test_unknown_frontend_path_serves_index(self, tmp_path):
        files = self._files(tmp_path)
        scope = {"type": "http", "method": "GET", "headers": []}
        response = await files.get_response("article/123", scope)
        assert response.status_code == 200
        assert response.path.endswith("index.html")
