"""``scripts/jwt_hs256_tool.py`` 的回归测试。

这个工具是**安全相关**的：它决定一枚 token 算不算通过。所以每一条拒绝规则
都要有独立用例钉住——尤其是 alg confusion（``alg: none``、算法拼写变体、
跨算法降级）这三类经典绕过。这些用例的命名直接写明「什么情况下必须拒绝」。

全部用例不需要网络。
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import jwt_hs256_tool as jwt  # noqa: E402

SECRET = "unit-test-secret"


def b64(data: dict) -> str:
    return jwt.b64url_encode(json.dumps(data, separators=(",", ":")).encode())


# ======================================================================
# 1. base64url 与结构
# ======================================================================
def test_b64url_roundtrip_without_padding():
    """JWT 用去填充的 base64url；解码时必须把 ``=`` 补回来。"""
    raw = b'{"a":1}'
    encoded = jwt.b64url_encode(raw)
    assert "=" not in encoded
    assert jwt.b64url_decode(encoded) == raw


def test_b64url_decode_restores_stripped_padding():
    """JWT 用去填充的 base64url；解码时必须把 ``=`` 补回来。

    合法余数只有 0 / 2 / 3（4 的倍数之外）。**长度余 1 的 base64 本身就是非法**，
    不能"努力猜一下"——那样会把乱码当数据放过去。
    """
    assert jwt.b64url_decode("") == b""
    assert jwt.b64url_decode("YQ") == b"a"
    assert jwt.b64url_decode("YWJj") == b"abc"
    assert jwt.b64url_decode(jwt.b64url_encode(b'{"a":1}')) == b'{"a":1}'
    with pytest.raises(jwt.JWTError, match="base64url 解码失败"):
        jwt.b64url_decode("a")


def test_split_token_requires_exactly_three_segments():
    """两段（``alg: none`` 的写法之一）或四段（JWE）都不能"猜"，直接报错。"""
    with pytest.raises(jwt.JWTError, match="三段"):
        jwt.split_token("aaa.bbb")
    with pytest.raises(jwt.JWTError, match="三段"):
        jwt.split_token("a.b.c.d")


def test_split_token_strips_bearer_prefix():
    """``Authorization: Bearer x`` 与裸 token 都要能吃。"""
    token = jwt.sign({"sub": "x"}, SECRET)
    assert jwt.split_token(f"Bearer {token}") == jwt.split_token(token)


def test_decode_rejects_non_json_segment():
    bad = f"{jwt.b64url_encode(b'not json')}.{b64({'sub': 'x'})}.AAAA"
    with pytest.raises(jwt.JWTError, match="不是合法 JSON"):
        jwt.decode(bad)


def test_decode_marks_time_claims():
    """``exp``/``iat`` 要被标注成人可读时间，便于肉眼核对过期。"""
    token = jwt.sign({"sub": "x"}, SECRET, expires_in=3600)
    info = jwt.decode(token)
    assert {c["claim"] for c in info["claims"]} == {"iat", "exp"}
    assert info["claims"][1]["relative"].endswith("后")


# ======================================================================
# 2. 验签正路
# ======================================================================
@pytest.mark.parametrize("alg", ["HS256", "HS384", "HS512"])
def test_sign_verify_roundtrip_for_every_algorithm(alg: str):
    token = jwt.sign({"sub": "roundtrip"}, SECRET, alg=alg, expires_in=60)
    result = jwt.verify(token, SECRET, alg=alg)
    assert result["ok"], result["reason"]
    assert result["payload"]["sub"] == "roundtrip"


def test_wrong_secret_fails():
    token = jwt.sign({"sub": "x"}, SECRET)
    result = jwt.verify(token, "another-secret")
    assert not result["ok"]
    assert result["reason"] == "签名不匹配"


def test_tampered_payload_fails():
    """改了 payload 却沿用原签名 —— 必须失败，这是 JWT 的全部意义所在。"""
    token = jwt.sign({"role": "reader"}, SECRET)
    header_b64, payload_b64, signature_b64 = jwt.split_token(token)
    body = json.loads(jwt.b64url_decode(payload_b64))
    body["role"] = "admin"
    forged = f"{header_b64}.{b64(body)}.{signature_b64}"
    result = jwt.verify(forged, SECRET)
    assert not result["ok"]
    assert result["reason"] == "签名不匹配"


def test_tampered_signature_fails():
    token = jwt.sign({"sub": "x"}, SECRET)
    broken = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert not jwt.verify(broken, SECRET)["ok"]


# ======================================================================
# 3. 时间声明
# ======================================================================
def test_expired_token_is_rejected():
    token = jwt.sign({"sub": "x"}, SECRET, expires_in=-5)
    result = jwt.verify(token, SECRET)
    assert not result["ok"]
    assert "已过期" in result["reason"]


def test_not_before_in_future_is_rejected():
    token = jwt.sign({"sub": "x", "nbf": int(time.time()) + 600}, SECRET)
    result = jwt.verify(token, SECRET)
    assert not result["ok"]
    assert "尚未生效" in result["reason"]


def test_leeway_lets_a_just_expired_token_through():
    """时钟偏移容错：刚过期几秒、给足 leeway 就应放行。"""
    token = jwt.sign({"sub": "x"}, SECRET, expires_in=-5)
    assert not jwt.verify(token, SECRET, leeway=0)["ok"]
    assert jwt.verify(token, SECRET, leeway=60)["ok"]


def test_check_time_false_still_verifies_signature():
    """只验签名时必须仍然拒绝改过的签名——关掉时间校验不等于关掉验签。

    注意：不能靠改签名段**最后一个字符**来伪造。base64url 的末位字符带
    填充位（32 字节 HMAC 编码成 43 字符，末字符只有高 4 位有效），
    相邻字符解码后字节相同——那样改出来的 token 依然验签通过，断言会假绿。
    这里改用「另一个密钥签出的签名段」替换，改动确定生效。
    """
    token = jwt.sign({"sub": "x"}, SECRET, expires_in=-5)
    assert jwt.verify(token, SECRET, check_time=False)["ok"]

    header, payload, _sig = jwt.split_token(token)
    _, _, foreign_sig = jwt.split_token(jwt.sign({"sub": "x"}, SECRET + "-wrong", expires_in=-5))
    broken = f"{header}.{payload}.{foreign_sig}"

    assert foreign_sig != _sig, "两段签名不该相同，否则这条测试没有意义"
    assert not jwt.verify(broken, SECRET, check_time=False)["ok"]


# ======================================================================
# 4. 算法绕过（安全核心）
# ======================================================================
def test_alg_none_is_rejected_even_when_explicitly_allowed():
    """``alg: none`` 是 alg confusion 的标志，**任何参数下都不通过**。

    ``allow_none`` 只换更明确的说明文字，不改变判定——否则这个开关本身
    就成了一个新的绕过入口。
    """
    none_token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'admin'})}."
    for allow in (False, True):
        result = jwt.verify(none_token, SECRET, allow_none=allow)
        assert not result["ok"], f"allow_none={allow} 时竟然通过了"
        assert "alg=none" in result["reason"]


def test_alg_spelling_variant_is_rejected():
    """``hS256`` / ``HS256 `` 这类变体必须拒绝。

    有些库比算法时忽略大小写或去空格，于是 ``hS256`` 会被当成 ``HS256``；
    本工具做精确匹配。
    """
    for weird in ("hS256", "HS256 ", " HS256", "hs256", "None"):
        token = f"{b64({'alg': weird, 'typ': 'JWT'})}.{b64({'sub': 'x'})}.AAAA"
        result = jwt.verify(token, SECRET)
        assert not result["ok"], f"算法 {weird!r} 竟然通过了"
        assert "白名单" in result["reason"]


def test_cross_algorithm_downgrade_is_rejected():
    """token 自称 HS512、调用方期望 HS256 时必须拒绝，不能按 token 自述换算法。"""
    token = jwt.sign({"sub": "x"}, SECRET, alg="HS512")
    result = jwt.verify(token, SECRET, alg="HS256")
    assert not result["ok"]
    assert "算法不符" in result["reason"]


def test_sign_rejects_unsupported_algorithm():
    with pytest.raises(jwt.JWTError, match="不支持的算法"):
        jwt.sign({"sub": "x"}, SECRET, alg="RS256")


# ======================================================================
# 5. CLI 自检
# ======================================================================
def test_cli_selftest_passes():
    """``selftest`` 子命令是交付物的一部分，必须自己先全绿。"""
    assert jwt.main(["selftest"]) == 0


def test_cli_verify_and_sign_roundtrip(capsys):
    token = jwt.sign({"sub": "cli"}, SECRET, expires_in=60)
    assert jwt.main(["verify", token, "--secret", SECRET, "--alg", "HS256"]) == 0
    assert jwt.main(["verify", token, "--secret", "wrong"]) == 1
    capsys.readouterr()  # 丢掉前两条命令的输出，只解析 decode 那条
    assert jwt.main(["decode", token, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["header"]["alg"] == "HS256"
    assert payload["payload"]["sub"] == "cli"


def test_cli_verify_reads_secret_from_env(monkeypatch):
    monkeypatch.setenv("JWT_TEST_SECRET", SECRET)
    token = jwt.sign({"sub": "env"}, SECRET, expires_in=60)
    assert jwt.main(["verify", token, "--secret-env", "JWT_TEST_SECRET"]) == 0


def test_cli_reports_missing_secret_env(monkeypatch, capsys):
    monkeypatch.delenv("JWT_TEST_SECRET", raising=False)
    token = jwt.sign({"sub": "env"}, SECRET)
    assert jwt.main(["verify", token, "--secret-env", "JWT_TEST_SECRET"]) == 1
    assert "未设置" in capsys.readouterr().err


def test_no_secret_recovery_code_path_exists():
    """工具里**不应该**存在任何密钥爆破/字典/长度扩展的入口。

    这是一条"负向"断言，防的是以后有人"顺手"加个 --wordlist。
    """
    source = (ROOT / "scripts" / "jwt_hs256_tool.py").read_text(encoding="utf-8")
    for banned in ("wordlist", "brute", "dictionary", "itertools.product", "crack"):
        assert banned not in source.lower(), f"出现了不该有的能力：{banned}"


def test_base64_module_import_is_used_not_shadowed():
    """``base64`` 必须来自标准库——防的是有人塞一个本地同名模块进来。"""
    assert Path(base64.__file__).name == "base64.py"
