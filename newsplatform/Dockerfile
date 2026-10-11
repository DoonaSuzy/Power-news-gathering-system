# ---- 统一镜像：采集层(newsagg) + 数据中台(newsplatform) + DDL(sql) ----
# 同一个镜像由 docker-compose 用不同 command 区分 fastapi / scheduler / worker，
# 避免三份镜像各装一遍 scrapy + sqlalchemy（VM 只有 ~3.8G 内存，磁盘也紧张）。
# pip 走清华源：部署机在国内网络下直连 PyPI 会超时。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# 非 root 运行；固定 uid 便于挂载卷的属主对齐
RUN useradd -m -u 10001 appuser

WORKDIR /app

# 依赖先装（层缓存：改业务代码不触发重装）
COPY requirements.txt /app/requirements.txt
COPY newsplatform/requirements.txt /app/newsplatform-requirements.txt
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements.txt \
    -r newsplatform-requirements.txt

COPY newsagg/ /app/newsagg/
COPY newsplatform/ /app/newsplatform/
COPY sql/ /app/sql/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 存活探针打 /api/v1/health（免鉴权，DB 不可用时返回 503 → 容器标记 unhealthy）
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status==200 else 1)"

# 默认起 API；用 uvicorn 而非 gunicorn——gunicorn 不在依赖清单里，加它等于凭空多一层
CMD ["uvicorn", "newsplatform.main:app", "--host", "0.0.0.0", "--port", "8000"]
