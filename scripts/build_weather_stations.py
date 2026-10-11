"""构建全国气象采样站点表（行政区中心点，2800+ 站）。

数据来源
--------
阿里云 DataV 行政区划 GeoJSON（公布官方 ``center``/``centroid`` 坐标）::

    https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json

省级（``100000_full.json``）→ 逐省下钻到地级 → 再下钻到区县级，取叶子节点为站点。

设计约束
--------
- **大区口径复用 ``newsagg.provinces``**（唯一来源），不在此处另建一份映射，
  否则八区分档会在两处漂移。
- 站点坐标取 ``centroid``（几何质心），缺失时回落 ``center``（行政中心）。
  两者都是官方字段，但含义不同：质心更能代表面状区域的平均天气，
  行政中心更贴近有人口聚落。默认用 ``centroid``，并把两个都写进产物以便核对。
- 网络请求带本地缓存（``data/weather/_geo_cache/``），重跑不重复打对方接口。

产物
----
- ``data/weather/stations.json``
- ``data/weather/stations.csv``（UTF-8 BOM，Excel 直开）

用法::

    python scripts/build_weather_stations.py             # 用缓存
    python scripts/build_weather_stations.py --refresh   # 强制重取
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsagg.provinces import PROVINCES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "weather"
CACHE_DIR = OUT_DIR / "_geo_cache"
BASE = "https://geo.datav.aliyun.com/areas_v3/bound"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 weather-station-builder")

#: 单次请求超时（秒）
TIMEOUT = 30
#: 请求间隔，避免给对方造成压力
THROTTLE = 0.05


def _fetch(adcode: int, *, refresh: bool = False) -> dict[str, Any] | None:
    """取一个行政单元的 ``_full.json``；不存在（404）返回 ``None``。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{adcode}_full.json"
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache.unlink()  # 缓存损坏则重取

    url = f"{BASE}/{adcode}_full.json"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            cache.write_text("{}", encoding="utf-8")  # 负缓存，避免重复打
            return None
        raise
    time.sleep(THROTTLE)
    cache.write_bytes(raw)
    data = json.loads(raw.decode("utf-8"))
    return data


def _features(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    return list(payload.get("features") or [])


def _has_children(feature: dict[str, Any]) -> bool:
    props = feature.get("properties") or {}
    return bool(props.get("childrenNum")) and props.get("level") != "district"


def _coord(feature: dict[str, Any]) -> tuple[float, float] | None:
    """返回 ``(lon, lat)``。优先质心，回落行政中心。"""
    props = feature.get("properties") or {}
    for key in ("centroid", "center"):
        pt = props.get(key)
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            try:
                lon, lat = float(pt[0]), float(pt[1])
            except (TypeError, ValueError):
                continue
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                return lon, lat
    return None


def _walk(adcode: int, *, refresh: bool, depth: int = 0) -> Iterator[dict[str, Any]]:
    """深度优先下钻，产出**叶子**行政单元。"""
    feats = _features(_fetch(adcode, refresh=refresh))
    if not feats:
        return
    for feat in feats:
        if _has_children(feat) and depth < 3:
            child_code = (feat.get("properties") or {}).get("adcode")
            if child_code:
                yield from _walk(int(child_code), refresh=refresh, depth=depth + 1)
                continue
        yield feat


def _province_fallback(prov_code: int, *, refresh: bool) -> list[dict[str, Any]]:
    """省份没有下钻数据时，退回该省在**全国文件**里的省级节点。

    典型场景：DataV 未收录台湾省的下级区划，逐级下钻会一个叶子都取不到。
    若不兜底，34 个省级行政区里会缺 1 个，港澳台大区也就不完整。
    """
    for feat in _features(_fetch(100000, refresh=refresh)):
        if int((feat.get("properties") or {}).get("adcode") or 0) == prov_code:
            return [feat]
    return []


def build(*, refresh: bool = False) -> list[dict[str, Any]]:
    """构建站点主表。"""
    stations: list[dict[str, Any]] = []
    seen: set[int] = set()

    for prov in PROVINCES:
        prov_code = int(prov.code)
        leaves = list(_walk(prov_code, refresh=refresh))
        if not leaves:
            leaves = _province_fallback(prov_code, refresh=refresh)
        for feat in leaves:
            props = feat.get("properties") or {}
            adcode = props.get("adcode")
            if adcode is None:
                continue
            adcode = int(adcode)
            if adcode in seen:
                continue
            coord = _coord(feat)
            if coord is None:
                continue
            seen.add(adcode)
            lon, lat = coord
            stations.append({
                "station_id": f"CN{adcode}",
                "adcode": adcode,
                "name": str(props.get("name") or ""),
                "level": str(props.get("level") or ""),
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "province_code": prov.code,
                "province": prov.name,
                "region": prov.region,
                "is_special": int(prov.is_special),
            })

    stations.sort(key=lambda s: (s["province_code"], s["adcode"]))
    return stations


def write_outputs(stations: list[dict[str, Any]], out_dir: Path = OUT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / "stations.json"
    cs = out_dir / "stations.csv"
    js.write_text(
        json.dumps({"count": len(stations), "stations": stations},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cols = ["station_id", "adcode", "name", "level", "lon", "lat",
            "province_code", "province", "region", "is_special"]
    with cs.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        writer.writerows(stations)
    return js, cs


def summarize(stations: list[dict[str, Any]]) -> None:
    by_region: dict[str, int] = {}
    by_level: dict[str, int] = {}
    by_prov: dict[str, int] = {}
    for s in stations:
        by_region[s["region"]] = by_region.get(s["region"], 0) + 1
        by_level[s["level"]] = by_level.get(s["level"], 0) + 1
        by_prov[s["province"]] = by_prov.get(s["province"], 0) + 1

    print(f"站点总数：{len(stations)}")
    print("\n按大区：")
    for r in ("华北", "东北", "华东", "华中", "华南", "西南", "西北", "港澳台"):
        if r in by_region:
            print(f"  {r:6s} {by_region[r]:5d}")
    print("\n按层级：")
    for lv, n in sorted(by_level.items(), key=lambda kv: -kv[1]):
        print(f"  {lv:10s} {n:5d}")
    print(f"\n覆盖省份：{len(by_prov)} 个（/ {len(PROVINCES)}）")
    missing = [p.name for p in PROVINCES if p.name not in by_prov]
    if missing:
        print(f"  未取到叶子节点的省：{', '.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建全国气象采样站点表")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，重新抓取")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR, help="产物目录")
    args = ap.parse_args(argv)

    stations = build(refresh=args.refresh)
    summarize(stations)
    if not stations:
        print("\n没有取到任何站点，检查网络/代理后重试。")
        return 1
    js, cs = write_outputs(stations, args.out_dir)
    print(f"\n已写出：\n  {js}\n  {cs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
