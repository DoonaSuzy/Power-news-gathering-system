#!/usr/bin/env python3
"""JWT 解码 / HS256·HS384·HS512 验签 / 签发 工具 —— 纯标准库。

用法::

    # 1) 解码（不需要密钥）：看 header / payload / 签名，并标注时间字段
    python jwt_hs256_tool.py decode eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.xxxxx
    python jwt_hs256_tool.py decode --file token.txt

    # 2) 验签（需要密钥）
    python jwt_hs256_tool.py verify <token> --secret mysecret
    python jwt_hs256_tool.py verify <token> --secret-env JWT_SECRET --alg HS256

    # 3) 签发（自测 / 造测试向量用）
    python jwt_hs256_tool.py sign --payload '{"sub":"test","role":"reader"}' --secret s --exp 3600

    # 4) 自检：本地造一枚 token 再解再验，确认工具本身没坏
    python jwt_hs256_tool.py selftest

安全约定（本工具刻意做成「不会帮你做危险的事」）
------------------------------------------------
1. **拒绝 ``alg: none``** 验签。这是 JWT 最经典的绕过手法（alg confusion）：
   攻击者把 header 改成 ``{"alg":"none"}`` 并去掉签名，服务端若照 header 走就放行。
   本工具除非显式 ``--allow-none``（且仍会判为**不通过**），一律拒签。
2. **算法白名单**：只认 ``HS256`` / ``HS384`` / ``HS512`` 三种精确拼写。
   ``hS256`` / ``HS256 `` 这类变体一律拒绝——有些库在算法比较时不区分大小写。
3. **恒定时间比较**：用 :func:`hmac.compare_digest`，不用 ``==``，避免签名被逐字节试探。
4. **不做密钥恢复**。本工具不包含、也不会实现任何爆破 / 字典 / 长度扩展逻辑。
   它只用于你**本来就合法持有**的 token 的解码与验签。
5. **默认不打印密钥**。``--secret-env`` 比 ``--secret`` 更好：命令行参数会进 shell 历史
   与进程列表。

退出码：0 通过 / 1 不通过或出错。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "ALGS",
    "JWTError",
    "b64url_decode",
    "b64url_encode",
    "decode",
    "sign",
    "split_token",
    "verify",
]


class JWTError(ValueError):
    """token 结构/算法/签名相关错误。"""


#: 支持的算法 → (hashlib 名, 期望签名字节长度)。**精确拼写**，不做大小写归一。
ALGS: dict[str, tuple[str, int]] = {
    "HS256": ("sha256", 32),
    "HS384": ("sha384", 48),
    "HS512": ("sha512", 64),
}

#: 时间类声明 → 中文说明（decode 时标注，便于肉眼核对过期）。
_TIME_CLAIMS = {
    "exp": "过期时间",
    "nbf": "生效时间",
    "iat": "签发时间",
    "auth_time": "认证时间",
    "updated_at": "更新时间",
}


# ======================================================================
# base64url
# ======================================================================
def b64url_encode(raw: bytes) -> str:
    """标准 base64url 且**去填充**（JWT 的写法）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(segment: str) -> bytes:
    """解 base64url，自动补回被去掉的 ``=`` 填充。

    Raises:
        JWTError: 含非法字符或长度无法补齐。
    """
    if not isinstance(segment, str):
        raise JWTError(f"期望字符串，收到 {type(segment).__name__}")
    text = segment.strip()
    if not text:
        return b""
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise JWTError(f"base64url 解码失败：{exc}") from exc


def _json_loads(raw: bytes, what: str) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise JWTError(f"{what} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise JWTError(f"{what} 应为 JSON 对象，实际是 {type(data).__name__}")
    return data


# ======================================================================
# 结构
# ======================================================================
def split_token(token: str) -> tuple[str, str, str]:
    """拆成 ``(header_b64, payload_b64, signature_b64)``。

    **必须是三段**。两段（``alg: none`` 的写法之一）或四段（JWE）都直接报错，
    不"猜"用户想表达什么。
    """
    text = (token or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    parts = text.split(".")
    if len(parts) != 3:
        raise JWTError(f"JWT 应为三段（header.payload.signature），实际 {len(parts)} 段")
    return parts[0], parts[1], parts[2]


def decode(token: str) -> dict[str, Any]:
    """解码 token 的 header / payload / 签名原文。**不验签**。

    Returns:
        ``{"header": {...}, "payload": {...}, "signature_b64": str,
        "signing_input": bytes, "alg": str, "claims": [...]}``
    """
    header_b64, payload_b64, signature_b64 = split_token(token)
    header = _json_loads(b64url_decode(header_b64), "header")
    payload = _json_loads(b64url_decode(payload_b64), "payload")
    alg = header.get("alg")

    signature = b64url_decode(signature_b64)
    claims: list[dict[str, Any]] = []
    now = int(time.time())
    for name, label in _TIME_CLAIMS.items():
        if name in payload:
            value = payload[name]
            if isinstance(value, (int, float)):
                moment = datetime.fromtimestamp(float(value), tz=UTC)
                claims.append(
                    {
                        "claim": name,
                        "label": label,
                        "raw": value,
                        "utc": moment.isoformat(),
                        "relative": _relative(float(value) - now),
                    }
                )
    return {
        "header": header,
        "payload": payload,
        "header_b64": header_b64,
        "payload_b64": payload_b64,
        "signature_b64": signature_b64,
        "signature_bytes": signature,
        "signing_input": f"{header_b64}.{payload_b64}".encode("ascii"),
        "alg": alg,
        "claims": claims,
    }


def _relative(seconds: float) -> str:
    if seconds >= 0:
        return f"{_human(seconds)}后"
    return f"{_human(-seconds)}前"


def _human(seconds: float) -> str:
    seconds = abs(int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"
    return f"{seconds // 86400} 天 {(seconds % 86400) // 3600} 小时"


# ======================================================================
# 签名 / 验签
# ======================================================================
def _sign(alg: str, signing_input: bytes, secret: bytes) -> bytes:
    if alg not in ALGS:
        raise JWTError(f"不支持的算法 {alg!r}；只认 {sorted(ALGS)}（大小写必须精确匹配）")
    digest, _ = ALGS[alg]
    return hmac.new(secret, signing_input, getattr(hashlib, digest)).digest()


def sign(
    payload: dict[str, Any],
    secret: str | bytes,
    *,
    alg: str = "HS256",
    header: dict[str, Any] | None = None,
    expires_in: int | None = None,
) -> str:
    """签发一枚 HMAC 签名 JWT（自用 / 造测试向量）。

    Args:
        payload: 载荷。``expires_in`` 给了就往里写 ``exp``/``iat``。
        secret: 密钥。字符串按 UTF-8 编码。
        alg: 只支持 :data:`ALGS` 里的精确拼写。
        header: 额外 header 字段，会覆盖 ``alg``/``typ`` 之外的同名键。
        expires_in: 有效秒数。

    Returns:
        形如 ``xxx.yyy.zzz`` 的 token 字符串。
    """
    if alg not in ALGS:
        raise JWTError(f"不支持的算法 {alg!r}；只认 {sorted(ALGS)}")
    body = dict(payload)
    if expires_in is not None:
        now = int(time.time())
        body.setdefault("iat", now)
        body["exp"] = now + int(expires_in)

    head: dict[str, Any] = {"alg": alg, "typ": "JWT"}
    if header:
        head.update({k: v for k, v in header.items() if k not in {"alg", "typ"}})

    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    signing_input = (
        b64url_encode(json.dumps(head, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        + "."
        + b64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    ).encode("ascii")
    return f"{signing_input.decode('ascii')}.{b64url_encode(_sign(alg, signing_input, key))}"


def verify(
    token: str,
    secret: str | bytes,
    *,
    alg: str | None = None,
    allow_none: bool = False,
    leeway: int = 0,
    check_time: bool = True,
) -> dict[str, Any]:
    """验签并（可选）检查时间声明。

    Args:
        token: 待验 token。
        secret: 密钥。
        alg: 期望算法。``None`` 表示取 token header 里的 ``alg``——**只在你信任
            签发方时才这样用**；跨系统验签应当写死期望算法。
        allow_none: 允许 ``alg: none``（默认否）。**即便允许，也判不通过**，
            只是把错误信息换成更明确的说明。这样做是刻意的：调这个参数的人
            多半正在排查 alg confusion，需要看到原因而不是一个笼统的"不支持"。
        leeway: 时间校验的容错秒数（时钟偏移）。
        check_time: 是否校验 ``exp``/``nbf``。

    Returns:
        ``{"ok": bool, "reason": str, "header": {...}, "payload": {...}}``
    """
    result: dict[str, Any] = {"ok": False, "reason": "", "header": {}, "payload": {}}
    try:
        decoded = decode(token)
    except JWTError as exc:
        result["reason"] = f"结构非法：{exc}"
        return result

    header, payload = decoded["header"], decoded["payload"]
    result["header"], result["payload"] = header, payload
    token_alg = header.get("alg")

    if token_alg == "none":
        result["reason"] = (
            "token 声明 alg=none。这是 alg confusion 绕过的特征——"
            "任何声称 alg=none 的 token 一律**不通过**，无论 allow_none 取值。"
            if allow_none
            else "token 声明 alg=none（未授权：加 --allow-none 可看到更明确的说明）"
        )
        return result
    if not isinstance(token_alg, str) or token_alg not in ALGS:
        result["reason"] = f"算法不在白名单内：alg={token_alg!r}，只认 {sorted(ALGS)}"
        return result
    if alg is not None and token_alg != alg:
        result["reason"] = f"算法不符：token 是 {token_alg}，期望 {alg}（拒绝按 token 自述换算法）"
        return result

    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = _sign(token_alg, decoded["signing_input"], key)
    actual = decoded["signature_bytes"]
    if not hmac.compare_digest(expected, actual):
        result["reason"] = "签名不匹配"
        return result

    if check_time:
        now = time.time()
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and now > float(exp) + leeway:
            result["reason"] = f"已过期（exp={int(exp)}，早 {-int(now - float(exp))} 秒）"
            return result
        nbf = payload.get("nbf")
        if isinstance(nbf, (int, float)) and now + leeway < float(nbf):
            result["reason"] = f"尚未生效（nbf={int(nbf)}）"
            return result

    result["ok"] = True
    result["reason"] = "签名有效" + ("，时间声明通过" if check_time else "（未校验时间）")
    return result


# ======================================================================
# CLI
# ======================================================================
def _read_token(args: argparse.Namespace) -> str:
    if getattr(args, "file", None):
        with open(args.file, encoding="utf-8") as handle:
            return handle.read().strip()
    token = getattr(args, "token", None) or ""
    if not token and not sys.stdin.isatty():
        token = sys.stdin.read().strip()
    if not token:
        raise JWTError("没有拿到 token：用位置参数、--file，或从标准输入管道喂进来")
    return token


def _resolve_secret(args: argparse.Namespace) -> str:
    if getattr(args, "secret_env", None):
        value = os.environ.get(args.secret_env)
        if value is None:
            raise JWTError(f"环境变量 {args.secret_env!r} 未设置")
        return value
    if getattr(args, "secret", None) is not None:
        return args.secret
    if not sys.stdin.isatty():
        value = sys.stdin.readline().rstrip("\n")
        if value:
            return value
    raise JWTError("缺少密钥：用 --secret 或（推荐）--secret-env")


def _cmd_decode(args: argparse.Namespace) -> int:
    info = decode(_read_token(args))
    if args.json:
        print(
            json.dumps(
                {
                    "header": info["header"],
                    "payload": info["payload"],
                    "signature_b64": info["signature_b64"],
                    "signature_bytes": len(info["signature_bytes"]),
                    "signing_input": info["signing_input"].decode("ascii"),
                    "claims": info["claims"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("== 解码（未验签）==")
    print(f"alg : {info['alg']}  typ: {info['header'].get('typ')}")
    print(f"签名 {len(info['signature_bytes'])} 字节")
    print()
    print("-- header --")
    print(json.dumps(info["header"], ensure_ascii=False, indent=2))
    print("-- payload --")
    print(json.dumps(info["payload"], ensure_ascii=False, indent=2))
    if info["claims"]:
        print("-- 时间声明 --")
        for claim in info["claims"]:
            print(
                f"  {claim['claim']:<10}{claim['label']:<8}{claim['raw']:>12}  "
                f"{claim['utc']}  ({claim['relative']})"
            )
    print()
    print("注意：解码不等于验签。payload 是明文可改的，只有签名才说明它没被改过。")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    secret = _resolve_secret(args)
    result = verify(
        _read_token(args),
        secret,
        alg=args.alg,
        allow_none=args.allow_none,
        leeway=args.leeway,
        check_time=not args.no_time_check,
    )
    if args.json:
        print(json.dumps({"ok": result["ok"], "reason": result["reason"],
                          "header": result["header"], "payload": result["payload"]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"{'[PASS]' if result['ok'] else '[FAIL]'} {result['reason']}")
        if result["header"]:
            print(f"  alg={result['header'].get('alg')}  payload={json.dumps(result['payload'], ensure_ascii=False)}")
    return 0 if result["ok"] else 1


def _cmd_sign(args: argparse.Namespace) -> int:
    try:
        payload = json.loads(args.payload)
    except ValueError as exc:
        raise JWTError(f"--payload 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise JWTError("--payload 必须是 JSON 对象")
    token = sign(
        payload,
        _resolve_secret(args),
        alg=args.alg,
        expires_in=args.exp,
    )
    print(token)
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    """本地造 token → 解码 → 验签 → 逐项破坏，确认工具行为符合预期。"""
    secret = "selftest-secret-do-not-use"
    checks: list[tuple[str, bool, str]] = []

    token = sign({"sub": "selftest", "role": "reader"}, secret, expires_in=3600)
    info = decode(token)
    checks.append(("签发后 payload 往返一致", info["payload"]["sub"] == "selftest", info["payload"]["sub"]))
    checks.append(("header 写入 alg=HS256", info["header"]["alg"] == "HS256", str(info["header"])))

    ok = verify(token, secret)
    checks.append(("正确密钥验签通过", ok["ok"], ok["reason"]))

    bad = verify(token, "wrong-secret")
    checks.append(("错误密钥验签失败", not bad["ok"], bad["reason"]))

    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    tampered_res = verify(tampered, secret)
    checks.append(("改签名后失败", not tampered_res["ok"], tampered_res["reason"]))

    # payload 里改一个字符再拼回原签名
    h, p, s = split_token(token)
    body = json.loads(b64url_decode(p))
    body["role"] = "admin"
    forged = f"{h}.{b64url_encode(json.dumps(body, separators=(',', ':')).encode())}.{s}"
    forged_res = verify(forged, secret)
    checks.append(("改 payload 后失败", not forged_res["ok"], forged_res["reason"]))

    expired = sign({"sub": "x"}, secret, expires_in=-10)
    expired_res = verify(expired, secret)
    checks.append(("过期 token 被拒", not expired_res["ok"], expired_res["reason"]))

    none_token = f"{b64url_encode(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}." \
                 f"{b64url_encode(json.dumps({'sub': 'x'}).encode())}."
    none_res = verify(none_token, secret)
    checks.append(("alg=none 一律不通过", not none_res["ok"], none_res["reason"]))

    # 直接构造一个 header 拼写为 hS256 的 token：有些库在算法比较时忽略大小写，
    # 于是 "hS256" 会被当成 "HS256" 放行。本工具精确匹配，应当拒绝。
    confusable_header = b64url_encode(json.dumps({"alg": "hS256", "typ": "JWT"}).encode())
    confusable_body = b64url_encode(json.dumps({"sub": "x"}).encode())
    confusable = f"{confusable_header}.{confusable_body}.AAAA"
    conf_res = verify(confusable, secret)
    checks.append(("alg 拼写变体被拒", not conf_res["ok"], conf_res["reason"]))

    alg_pin = verify(sign({"a": 1}, secret, alg="HS512"), secret, alg="HS256")
    checks.append(("跨算法降级被拒", not alg_pin["ok"], alg_pin["reason"]))

    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}  -> {detail}")
    failed = [c for c in checks if not c[1]]
    print(f"\n  自检 {len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jwt_hs256_tool.py",
        description="JWT 解码 / HS256·HS384·HS512 验签 / 签发（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python jwt_hs256_tool.py decode <token>\n"
            "  python jwt_hs256_tool.py verify <token> --secret-env JWT_SECRET --alg HS256\n"
            "  python jwt_hs256_tool.py sign --payload '{\"sub\":\"a\"}' --secret s --exp 60\n"
            "  python jwt_hs256_tool.py selftest\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_secret(p: argparse.ArgumentParser) -> None:
        p.add_argument("--secret", help="密钥（会进 shell 历史，能不用就不用）")
        p.add_argument("--secret-env", metavar="VAR", help="从环境变量读密钥（推荐）")

    dec = sub.add_parser("decode", help="解码 header/payload（不验签）")
    dec.add_argument("token", nargs="?", help="JWT；省略则读 --file 或标准输入")
    dec.add_argument("--file", help="从文件读 token")
    dec.add_argument("--json", action="store_true", help="以 JSON 输出")
    dec.set_defaults(func=_cmd_decode)

    ver = sub.add_parser("verify", help="验签 + 时间声明校验")
    ver.add_argument("token", nargs="?", help="JWT；省略则读 --file 或标准输入")
    ver.add_argument("--file", help="从文件读 token")
    ver.add_argument("--alg", choices=sorted(ALGS), default=None,
                     help="期望算法（强烈建议写死；默认取 token 自述）")
    ver.add_argument("--allow-none", action="store_true",
                     help="允许 alg=none（仍然判不通过，只是把原因说清楚）")
    ver.add_argument("--leeway", type=int, default=0, help="时间校验容错秒数（默认 0）")
    ver.add_argument("--no-time-check", action="store_true", help="只验签名，不校验 exp/nbf")
    ver.add_argument("--json", action="store_true", help="以 JSON 输出")
    add_secret(ver)
    ver.set_defaults(func=_cmd_verify)

    sig = sub.add_parser("sign", help="签发一枚 HMAC JWT（自用/造测试向量）")
    sig.add_argument("--payload", required=True, help="JSON 对象字符串")
    sig.add_argument("--alg", choices=sorted(ALGS), default="HS256")
    sig.add_argument("--exp", type=int, default=None, metavar="SECONDS", help="有效期秒数")
    add_secret(sig)
    sig.set_defaults(func=_cmd_sign)

    self_test = sub.add_parser("selftest", help="本地自检（不需要网络）")
    self_test.set_defaults(func=_cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except JWTError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
