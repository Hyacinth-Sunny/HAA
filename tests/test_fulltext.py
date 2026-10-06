"""Tests for haa/llm/fulltext.py — 全 mock，零网络（v1.0.6-rev3）。"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from haa.llm import fulltext as ft
from haa.llm.fulltext import (
    FulltextError,
    find_fulltext,
    find_in_local_library,
    get_fulltext,
    _assert_public_url,
    _safe_xml_fromstring,
)


# --- 出站安全闸 ----------------------------------------------------------------


def test_assert_public_url_rejects_scheme():
    with pytest.raises(FulltextError, match="scheme"):
        _assert_public_url("file:///etc/passwd")


def test_assert_public_url_rejects_localhost_and_private_literal():
    with pytest.raises(FulltextError, match="internal hostname"):
        _assert_public_url("http://localhost:8420/x")
    with pytest.raises(FulltextError, match="non-public"):
        _assert_public_url("http://192.168.1.5/a.pdf")


def test_assert_public_url_rejects_domain_resolving_to_private(monkeypatch):
    """DNS 解析到内网 IP（rebinding/投毒）必须拒——只查主机名字面量不够。"""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 1234))],
    )
    with pytest.raises(FulltextError, match="resolves to non-public"):
        _assert_public_url("https://evil.example.com/a.pdf")


def test_assert_public_url_allows_public(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    assert _assert_public_url("https://export.arxiv.org/api/query") .startswith("https://")


# --- XML 安全 ------------------------------------------------------------------


def test_safe_xml_rejects_entity():
    with pytest.raises(FulltextError, match="ENTITY"):
        _safe_xml_fromstring(b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>")


def test_safe_xml_accepts_jats_doctype():
    """PMC JATS 合法携带 DOCTYPE 声明（真实冒烟实证）——不得误杀。"""
    raw = b"<?xml version='1.0'?><!DOCTYPE article PUBLIC '-//NLM//DTD JATS//EN' 'jats.dtd'><article><body>text</body></article>"
    root = _safe_xml_fromstring(raw)
    assert root.tag == "article"


def test_safe_xml_accepts_plain():
    root = _safe_xml_fromstring(b"<feed><entry><id>http://x/abs/1</id></entry></feed>")
    assert root.tag == "feed"


# --- fetch_pdf（mock httpx） -----------------------------------------------------


class _FakeStream:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_bytes(self, n):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    """按脚本吐响应的 httpx.Client 替身：responses 是 stream 结果列表。"""

    responses: list = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url):
        item = _FakeClient.responses.pop(0) if _FakeClient.responses else _FakeStream([b""], status=500)
        return item if isinstance(item, _FakeStream) else item()


def _patch_httpx(monkeypatch, streams):
    import httpx

    _FakeClient.responses = list(streams)
    monkeypatch.setattr(ft, "_resolve_redirects", lambda url: url)
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(ft.time, "sleep", lambda s: None)


def test_fetch_pdf_retries_then_succeeds(tmp_path, monkeypatch):
    corrupt = _FakeStream([b"<html>403 page</html>"])          # 第一次：错误页（非 %PDF）
    ok = _FakeStream([b"%PDF-1.4 fake", b"x" * 4096])          # 第二次：真 PDF（>1KB 过大小校验）
    _patch_httpx(monkeypatch, [corrupt, ok])
    path = ft.fetch_pdf("https://export.arxiv.org/pdf/2401.00001", tmp_path, retries=2)
    assert path.exists() and path.read_bytes().startswith(b"%PDF")


def test_fetch_pdf_rejects_corrupt_and_cleans_partial(tmp_path, monkeypatch):
    _patch_httpx(monkeypatch, [_FakeStream([b"<html>blocked</html>", b"more"])] * 3)
    with pytest.raises(FulltextError, match="not a valid PDF|failed"):
        ft.fetch_pdf("https://x.example.com/a.pdf", tmp_path, retries=2)
    assert list(tmp_path.iterdir()) == []  # 半成品不留缓存


def test_fetch_pdf_cache_hit_skips_network(tmp_path, monkeypatch):
    import hashlib

    key = hashlib.sha256(b"https://a.example.com/p.pdf").hexdigest()[:24]
    cached = tmp_path / f"{key}.pdf"
    cached.write_bytes(b"%PDF-1.4 " + b"x" * 2048)
    _patch_httpx(monkeypatch, [])  # 无网络响应——若走网络必炸
    assert ft.fetch_pdf("https://a.example.com/p.pdf", tmp_path) == cached


# --- 本地论文库 ------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    d = tmp_path / "papers" / "ICML2026" / "Regular"
    d.mkdir(parents=True)
    (d / "R0001-Low-dimensional topology of deep neural networks.pdf").write_bytes(b"%PDF-1.4 " + b"0" * 2048)
    (d / "R0002-Target-Aware Bandit Allocation for Chemical Space.pdf").write_bytes(b"%PDF-1.4 " + b"0" * 2048)
    (d / "R0003-Duplicate Same Title.pdf").write_bytes(b"%PDF-1.4 " + b"0" * 2048)
    (d.parent.parent / "Dup" ).mkdir(exist_ok=True)
    (d.parent.parent / "Dup" / "R0009-Duplicate Same Title.pdf").write_bytes(b"%PDF-1.4 " + b"0" * 2048)
    return d.parent.parent


def test_local_library_title_match(library):
    hit = find_in_local_library("Low-Dimensional Topology of Deep Neural Networks", library)
    assert hit is not None and "R0001" in hit.name


def test_local_library_ambiguous_returns_none(library):
    assert find_in_local_library("Duplicate Same Title", library) is None


def test_local_library_no_match(library):
    assert find_in_local_library("Totally Unrelated Quantum Paper", library) is None


# --- 发现链顺序与容错 --------------------------------------------------------------


def test_find_fulltext_order_and_tolerant_sources(monkeypatch, library):
    calls = []

    monkeypatch.setattr(ft, "_find_via_openalex", lambda *a: (calls.append("openalex") or [ft.FulltextCandidate("openalex", "https://oa.example.com/1.pdf")]))
    monkeypatch.setattr(ft, "_find_via_arxiv", lambda *a: (calls.append("arxiv") or [ft.FulltextCandidate("arxiv", "https://export.arxiv.org/pdf/2")]))
    monkeypatch.setattr(ft, "_find_via_unpaywall", lambda *a: (_ for _ in ()).throw(RuntimeError("429")))  # 单源挂
    monkeypatch.setattr(ft, "_find_via_pmc", lambda *a: (calls.append("pmc") or []))
    monkeypatch.setattr(ft, "_find_via_s2", lambda *a: [])

    cands, errors = find_fulltext(
        title="Low-dimensional topology of deep neural networks",
        local_library_dir=str(library),
        email="a@b.com",
    )
    sources = [c.source for c in cands]
    # 用户指定顺序：local → openalex → arxiv（网络源里 OpenAlex 打头）
    assert sources[0] == "local"
    assert sources.index("openalex") < sources.index("arxiv")
    assert "unpaywall" in errors  # 单源失败入 errors 不炸链


def test_find_fulltext_local_disabled_when_dir_none(monkeypatch):
    monkeypatch.setattr(ft, "_find_via_openalex", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_arxiv", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_unpaywall", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_pmc", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_s2", lambda *a: [])
    cands, _ = find_fulltext(title="whatever")
    assert all(c.source != "local" for c in cands)


# --- get_fulltext -----------------------------------------------------------------


def test_get_fulltext_local_hit_needs_no_network(monkeypatch, library, tmp_path):
    """本地库命中：不触网直接出全文（extract_pdf_text 打桩）。"""
    monkeypatch.setattr("haa.llm.pdf_utils.extract_pdf_text", lambda p, m: "FULLTEXT " * 200)
    monkeypatch.setattr(ft, "_find_via_openalex", lambda *a: (_ for _ in ()).throw(AssertionError("should not hit network")))
    monkeypatch.setattr(ft, "_find_via_arxiv", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_unpaywall", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_pmc", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_s2", lambda *a: [])
    res = get_fulltext(
        title="Low-dimensional topology of deep neural networks",
        local_library_dir=str(library), email="a@b.com",
    )
    assert res.source.startswith("local:")
    assert res.method == "pdf"
    assert len(res.text) > 1000


def test_get_fulltext_all_fail_reports_per_source(monkeypatch):
    monkeypatch.setattr(ft, "_find_via_openalex", lambda *a: (_ for _ in ()).throw(RuntimeError("DNS fail")))
    monkeypatch.setattr(ft, "_find_via_arxiv", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_unpaywall", lambda *a: [ft.FulltextCandidate("unpaywall", "https://paywalled.example.com/x.pdf")])
    monkeypatch.setattr(ft, "_find_via_pmc", lambda *a: [])
    monkeypatch.setattr(ft, "_find_via_s2", lambda *a: [])
    monkeypatch.setattr(ft, "fetch_pdf", lambda *a, **k: (_ for _ in ()).throw(FulltextError("download failed")))
    with pytest.raises(FulltextError) as ei:
        get_fulltext(doi="10.1/x", email="a@b.com")
    msg = str(ei.value)
    assert "openalex" in msg and "DNS fail" in msg      # 逐源报因
    assert "unpaywall" in msg and "download failed" in msg
