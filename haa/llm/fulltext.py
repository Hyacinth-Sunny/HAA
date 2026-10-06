"""论文全文获取（v1.0.6-rev3）。

P1 审判端"仅凭摘要断言可行性/新颖性"的根治层。设计依据为本机（NAT 校园网）
2026-08-27 实测：

- **可下全文**：arXiv PDF（DNS 间歇需重试）、Unpaywall 指到的出版商 PDF
  （nature 等——须浏览器 UA + GET 跟随重定向，HEAD 会收到 303/204 假象）、
  Frontiers 直链、PMC eutils 全文 XML（OA 文章）、S2 PDF CDN（主机通）。
- **不可用**：OpenReview（Cloudflare Turnstile 会话门，登录 Cookie 过期即断，
  只能离线批量——见 V1.1 计划书 §3.9）、MDPI(403)、Springer 直链、ACM/IEEE。
- OpenAlex API DNS 间歇失败（用户判定为间歇性）——带重试、失败静默跳过。

发现链（``find_fulltext``，逐源容错）：

1. 本地论文库（opt-in，``tools.fulltext.local_library``）——标题 token 重合
   度匹配文件名，零网络零限流；库由 OpenClaw 等辅助离线采集（刊物目录结构，
   无领域分类，匹配不依赖分类）。
2. OpenAlex ``locations[]`` 全列表（用户指定置首）
3. arXiv title 检索（export API，abs→pdf）
4. Unpaywall（DOI → 全部 oa_location 的 pdf_url）
5. PMC eutils（OA 文章取 JATS 全文 XML，不走 pdftotext）
6. S2 openAccessPdf（429 容忍）

安全（Mimosa 约束）：所有出站请求经 ``_assert_public_url``——协议白名单
（http/https）+ 主机名检查 + **DNS 解析后逐 IP 阻断私网/环回/链路本地/保留
地址**（防 DNS rebinding）；重定向不走库自动跟随，手动逐跳校验（最多 5 跳）。
XML 解析前拒绝 DTD/ENTITY 声明并限大小（ElementTree 无 XXE 防护）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("haa.fulltext")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
    " HAA-research-bot"
)
_MAX_REDIRECTS = 5


class FulltextError(Exception):
    """全文获取失败（结构化：逐源原因见异常文本）。"""


@dataclass
class FulltextResult:
    """一次 get_fulltext 的结果。"""

    text: str
    source: str            # 来源标识：local:<文件名> / <域名> / pmc:<PMCID>
    url_or_path: str
    method: str            # pdf | pmc-xml
    chars: int


@dataclass
class FulltextCandidate:
    """find_fulltext 返回的候选（按优先级排序）。"""

    source: str            # local / openalex / arxiv / unpaywall / pmc / s2
    url_or_path: str       # PDF URL、本地路径、或 pmc:<PMCID> 特殊句柄
    method: str = "pdf"    # pdf | pmc-xml
    note: str = ""


# --- 出站安全闸（协议 + 主机名 + DNS 解析后 IP 边界 + 逐跳重定向） ------------


def _assert_public_url(url: str) -> str:
    """校验出站 URL：仅 http/https；主机名与**解析后的全部 IP** 不得为
    localhost/私网/环回/链路本地/保留/组播（防 SSRF 与 DNS rebinding）。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FulltextError(f"URL rejected: scheme {parsed.scheme!r} not allowed: {url!r}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise FulltextError(f"URL rejected: no hostname: {url!r}")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise FulltextError(f"URL rejected: internal hostname {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local \
                or ip.is_multicast or ip.is_unspecified:
            raise FulltextError(f"URL rejected: non-public address {host!r}")
        return url
    # 域名：解析 DNS 后逐 IP 检查（域名解析到内网 = rebinding/投毒，拒绝）
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise FulltextError(f"URL rejected: DNS resolution failed for {host!r}: {exc}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            a = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if a.is_private or a.is_loopback or a.is_reserved or a.is_link_local \
                or a.is_multicast or a.is_unspecified:
            raise FulltextError(
                f"URL rejected: {host!r} resolves to non-public address {addr}"
            )
    return url


def _resolve_redirects(url: str) -> str:
    """手动跟随重定向（每跳过 _assert_public_url），返回最终 URL。

    库自带的 follow_redirects 会绕过逐跳校验——3xx 目标同样可能是内网地址。
    """
    import httpx

    current = _assert_public_url(url)
    for _ in range(_MAX_REDIRECTS):
        with httpx.Client(
            follow_redirects=False, timeout=httpx.Timeout(20, connect=10),
            headers={"User-Agent": _UA},
        ) as client:
            r = client.head(current)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                current = _assert_public_url(urljoin(current, r.headers["location"]))
                continue
            return current
    return current


# --- 下载 ------------------------------------------------------------------


def fetch_pdf(
    url: str,
    dest_dir: str | Path,
    *,
    timeout_s: int = 90,
    max_mb: int = 30,
    retries: int = 2,
    backoff_base: float = 2.0,
) -> Path:
    """下载 PDF 到缓存目录（按 URL 哈希幂等），返回本地路径。

    - GET + 浏览器 UA + 手动逐跳重定向（实测 HEAD 有 303/204 假象，故探测
      用 HEAD、正文下载用 GET）
    - 重试 ``retries`` 次（arXiv DNS 间歇与流损坏实证需要）
    - ``%PDF`` 魔数校验：损坏流/错误页直接重试而非喂给 pdftotext 报天书
    - 已缓存（且非空）则直接复用——同论文多阶段只下一次，防限流
    """
    import httpx

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    cached = dest / f"{key}.pdf"
    if cached.exists() and cached.stat().st_size > 1024:
        return cached

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            final = _resolve_redirects(url)
            with httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_s, connect=10.0),
                headers={"User-Agent": _UA},
            ) as client:
                with client.stream("GET", final) as resp:
                    resp.raise_for_status()
                    size = 0
                    magic = b""
                    with cached.open("wb") as f:
                        for chunk in resp.iter_bytes(65536):
                            size += len(chunk)
                            if not magic and chunk:
                                magic = chunk[:5]
                            if size > max_mb * 1024 * 1024:
                                raise FulltextError(f"PDF exceeds {max_mb}MB cap: {url!r}")
                            f.write(chunk)
            if magic.startswith(b"%PDF") and size > 1024:
                return cached
            last_err = FulltextError(f"not a valid PDF (magic={magic!r}, {size}B): {url!r}")
        except Exception as exc:  # 下载/校验失败统一走重试
            last_err = exc
        cached.unlink(missing_ok=True)  # 失败的半成品不留缓存
        if attempt < retries:
            time.sleep(backoff_base ** attempt)
            logger.info(
                "fulltext fetch retry %d/%d for %s: %s",
                attempt + 1, retries, url, last_err,
            )
    raise FulltextError(f"PDF download failed after {retries + 1} attempt(s): {last_err}")


# --- 本地论文库（opt-in） ----------------------------------------------------


_STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "in", "on", "with", "to", "via",
    "towards", "from", "by", "at", "is", "are",
}


def _title_tokens(text: str) -> set[str]:
    """规整化标题 → 有意义 token 集（小写、去序号前缀/停用词/短数字）。"""
    text = re.sub(r"^\d+\s*-", "", text.strip())          # "R0001-Title" / "01-Title"
    text = re.sub(r"\.(pdf|PDF)$", "", text)
    return {
        t for t in re.split(r"[^a-z0-9]+", text.lower())
        if len(t) > 1 and t not in _STOPWORDS and not t.isdigit()
    }


def find_in_local_library(title: str, library_dir: str | Path) -> Path | None:
    """标题模糊匹配本地库文件名（token 重合度 ≥0.6 且唯一最优）。

    库按刊物目录组织（无领域分类），匹配只看标题；歧义（两个文件并列最高）
    不赌，返回 None 交给在线链。
    """
    lib = Path(library_dir).expanduser()
    if not lib.is_dir():
        return None
    want = _title_tokens(title)
    if len(want) < 2:
        return None
    best_score, best_paths = 0.0, []
    for p in lib.rglob("*.pdf"):
        got = _title_tokens(p.stem)
        if not got:
            continue
        score = len(want & got) / max(len(want), len(got))
        if score > best_score:
            best_score, best_paths = score, [p]
        elif score == best_score and score > 0:
            best_paths.append(p)
    if best_score >= 0.6 and len(best_paths) == 1:
        return best_paths[0]
    return None


# --- 在线发现链（逐源容错） ---------------------------------------------------

_XML_MAX_BYTES = 20 * 1024 * 1024  # arXiv Atom / PMC JATS 正常远小于此


def _http_get_json(url: str, params: dict | None = None, timeout_s: int = 20) -> Any:
    import httpx

    _assert_public_url(url)
    with httpx.Client(timeout=timeout_s, headers={"User-Agent": _UA}) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _safe_xml_fromstring(raw: bytes) -> ET.Element:
    """解析不可信 XML 前拒绝实体声明（ElementTree 无 XXE 防护）。

    只拒 ``<!ENTITY``——实体扩展才是攻击向量；``<!DOCTYPE`` 是 PMC JATS
    的合法声明（真实冒烟实证一刀切会误杀全部 PMC 全文），ET 本身不加载
    外部 DTD，保留声明无害。
    """
    if len(raw) > _XML_MAX_BYTES:
        raise FulltextError(f"XML exceeds {_XML_MAX_BYTES}B cap")
    head = raw[:4096].lstrip()
    if b"<!ENTITY" in head:
        raise FulltextError("XML rejected: ENTITY declarations not allowed")
    return ET.fromstring(raw)


def _http_get_bytes(url: str, timeout_s: int = 20) -> bytes:
    import httpx

    _assert_public_url(url)
    with httpx.Client(
        timeout=timeout_s, headers={"User-Agent": _UA}, follow_redirects=False,
    ) as client:
        r = client.get(url)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            return _http_get_bytes(urljoin(url, _assert_public_url(r.headers["location"])), timeout_s)
        r.raise_for_status()
        return r.content[: _XML_MAX_BYTES + 1]


def _find_via_openalex(doi: str | None, title: str | None, email: str) -> list[FulltextCandidate]:
    out: list[FulltextCandidate] = []
    params: dict[str, Any] = {"per-page": 10}
    if email:
        params["mailto"] = email
    if doi:
        params["filter"] = f"doi:{doi}"
    elif title:
        params["search"] = title
    else:
        return out
    data = _http_get_json("https://api.openalex.eu/works", params) or {}
    for w in data.get("results", []) or []:
        for loc in w.get("locations", []) or []:
            pdf = (loc or {}).get("pdf_url")
            if pdf:
                out.append(FulltextCandidate("openalex", pdf))
    return out[:4]


def _find_via_arxiv(title: str) -> list[FulltextCandidate]:
    import urllib.parse

    if not title:
        return []
    url = (
        "https://export.arxiv.org/api/query?search_query=ti:"
        + urllib.parse.quote(f'"{title}"')
        + "&max_results=3"
    )
    root = _safe_xml_fromstring(_http_get_bytes(url))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[FulltextCandidate] = []
    for entry in root.findall("a:entry", ns):
        link = entry.findtext("a:id", "", ns) or ""
        if "/abs/" in link:
            pdf = link.replace("/abs/", "/pdf/")
            out.append(FulltextCandidate("arxiv", pdf, note=entry.findtext("a:title", "", ns)))
    return out


def _find_via_unpaywall(doi: str, email: str) -> list[FulltextCandidate]:
    if not doi or not email:
        return []
    data = _http_get_json(f"https://api.unpaywall.org/v2/{doi}", {"email": email}) or {}
    out: list[FulltextCandidate] = []
    for loc in [data.get("best_oa_location") or {}, *(data.get("oa_locations") or [])]:
        pdf = (loc or {}).get("url_for_pdf") or (loc or {}).get("pdf_url")
        if pdf:
            cand = FulltextCandidate("unpaywall", pdf)
            if not any(c.url_or_path == cand.url_or_path for c in out):
                out.append(cand)
    return out[:4]


def _find_via_pmc(doi: str | None, title: str | None) -> list[FulltextCandidate]:
    """PMC：esearch（DOI/title→PMCID）→ OA 文章可 efetch 全文 XML。

    返回 ``pmc:<PMCID>`` 特殊句柄候选（method=pmc-xml，不走 pdftotext）。
    真实冒烟实证：裸 title 词检索会命中不相关论文（静默给错全文比拿不到
    更毒）——必须用 [Title] 字段限定，且 get_fulltext 侧核验 DOI/标题一致。
    """
    params: dict[str, Any] = {"db": "pmc", "retmode": "json"}
    if doi:
        params["term"] = f"{doi}[DOI]"
    elif title:
        params["term"] = f"{title}[Title]"
    else:
        return []
    data = _http_get_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params
    ) or {}
    ids = (data.get("esearchresult") or {}).get("idlist") or []
    return [FulltextCandidate("pmc", f"pmc:{i}", method="pmc-xml") for i in ids[:2]]


def _find_via_s2(doi: str | None, title: str | None) -> list[FulltextCandidate]:
    params: dict[str, Any] = {"fields": "title,openAccessPdf"}
    if doi:
        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    elif title:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        params["query"] = title
        params["limit"] = 3
    else:
        return []
    data = _http_get_json(url, params) or {}
    papers = data.get("data") if isinstance(data, dict) and "data" in data else [data]
    out: list[FulltextCandidate] = []
    for p in papers or []:
        pdf = ((p or {}).get("openAccessPdf") or {}).get("url")
        if pdf:
            out.append(FulltextCandidate("s2", pdf))
    return out[:2]


def find_fulltext(
    doi: str | None = None,
    title: str | None = None,
    *,
    local_library_dir: str | Path | None = None,
    email: str = "",
) -> tuple[list[FulltextCandidate], dict[str, str]]:
    """六源发现链。返回（按优先级排序的候选列表, 逐源失败原因表）。

    顺序（用户指定）：本地库(opt-in) → OpenAlex → arXiv → Unpaywall →
    PMC → S2。逐源容错：单源失败记入 source_errors 不炸链。
    """
    candidates: list[FulltextCandidate] = []
    errors: dict[str, str] = {}

    if local_library_dir:
        try:
            hit = find_in_local_library(title or "", local_library_dir)
            if hit:
                candidates.append(FulltextCandidate("local", str(hit), note="local library"))
        except Exception as exc:
            errors["local"] = f"{type(exc).__name__}: {exc}"

    for name, fn in (
        ("openalex", lambda: _find_via_openalex(doi, title, email)),
        ("arxiv", lambda: _find_via_arxiv(title or "")),
        ("unpaywall", lambda: _find_via_unpaywall(doi or "", email)),
        ("pmc", lambda: _find_via_pmc(doi, title)),
        ("s2", lambda: _find_via_s2(doi, title)),
    ):
        try:
            got = fn()
            if got:
                candidates.extend(got)
            else:
                errors[name] = "no candidates"
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"

    return candidates, errors


# --- 提取与一站式入口 ---------------------------------------------------------


def _pmc_fulltext_xml(pmcid: str, max_chars: int) -> tuple[str, str, str]:
    """efetch PMC 全文 JATS XML → (纯文本, 元数据 DOI, 元数据标题)。

    元数据供调用方核验"返回的论文就是请求的论文"（esearch 模糊命中错论文
    的实证防线）。
    """
    pmcid = re.sub(r"[^A-Za-z0-9]", "", pmcid)  # PMCID 白名单化，防 URL 注入
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pmc&id={pmcid}&rettype=xml"
    )
    root = _safe_xml_fromstring(_http_get_bytes(url, timeout_s=30))
    parts = ["".join(body.itertext()).strip() for body in root.iter("body")]
    text = "\n\n".join(p for p in parts if p)
    if not text:
        raise FulltextError(f"PMC {pmcid}: no <body> in XML (likely non-OA)")
    meta_doi = ""
    for aid in root.iter("article-id"):
        if (aid.get("pub-id-type") or "") == "doi":
            meta_doi = (aid.text or "").strip().lower()
            break
    title_el = root.find(".//article-title")
    meta_title = "".join(title_el.itertext()).strip() if title_el is not None else ""
    return text[:max_chars], meta_doi, meta_title


def _title_match(expected: str, actual: str) -> float:
    """两标题 token 重合度（核验"返回论文==请求论文"）。"""
    want, got = _title_tokens(expected), _title_tokens(actual)
    if not want or not got:
        return 0.0
    return len(want & got) / max(len(want), len(got))


def get_fulltext(
    doi: str | None = None,
    title: str | None = None,
    *,
    cache_dir: str | Path | None = None,
    max_chars: int = 50000,
    timeout_s: int = 90,
    max_mb: int = 30,
    retries: int = 2,
    local_library_dir: str | Path | None = None,
    email: str = "",
) -> FulltextResult:
    """一站式：find → 逐候选取文 → 全文+来源；全败逐源报因。

    绝不静默降级成"只有摘要"，也绝不静默给**错误的全文**——title 路径的
    PMC/arXiv 候选核验元数据（DOI 精确相等 / 标题 token 重合 ≥0.5），不
    一致记错并试下一候选（esearch 模糊命中错论文的实证防线）。
    """
    if not doi and not title:
        raise FulltextError("get_fulltext: doi or title required")
    doi_l = (doi or "").strip().lower()
    candidates, errors = find_fulltext(
        doi, title, local_library_dir=local_library_dir, email=email
    )

    from haa.llm.pdf_utils import extract_pdf_text

    def _verify(expected_doi: str, expected_title: str, meta_doi: str, meta_title: str) -> None:
        if expected_doi and meta_doi:
            if meta_doi != expected_doi:
                raise FulltextError(f"paper mismatch: asked DOI {expected_doi!r}, got {meta_doi!r}")
            return
        if expected_title and meta_title:
            score = _title_match(expected_title, meta_title)
            if score < 0.5:
                raise FulltextError(
                    f"paper mismatch: title overlap {score:.2f} < 0.5 "
                    f"(asked {expected_title!r}, got {meta_title!r})"
                )

    fetch_errors: list[str] = []
    for cand in candidates:
        try:
            if cand.method == "pmc-xml":
                pmcid = cand.url_or_path.removeprefix("pmc:")
                text, meta_doi, meta_title = _pmc_fulltext_xml(pmcid, max_chars)
                _verify(doi_l, title or "", meta_doi, meta_title)
                return FulltextResult(text, f"pmc:{pmcid}", cand.url_or_path, "pmc-xml", len(text))
            if cand.source == "local":
                text = extract_pdf_text(cand.url_or_path, max_chars)
                return FulltextResult(
                    text, f"local:{Path(cand.url_or_path).name}", cand.url_or_path, "pdf", len(text)
                )
            if cand.source == "arxiv" and title and cand.note:
                # arXiv 检索结果自带标题——先核验再下载，省流量
                if _title_match(title, cand.note) < 0.5:
                    raise FulltextError(f"paper mismatch: arXiv hit {cand.note!r} ≠ asked {title!r}")
            if cache_dir is None:
                cache_dir = Path("data/campaigns") / "_adhoc" / "fulltext"
            pdf = fetch_pdf(
                cand.url_or_path, cache_dir,
                timeout_s=timeout_s, max_mb=max_mb, retries=retries,
            )
            text = extract_pdf_text(str(pdf), max_chars)
            if len(text.strip()) < 500:
                raise FulltextError(
                    f"extracted text too short ({len(text)} chars) — likely not a real fulltext"
                )
            host = urlparse(cand.url_or_path).hostname or cand.url_or_path
            return FulltextResult(text, host, cand.url_or_path, "pdf", len(text))
        except Exception as exc:
            fetch_errors.append(
                f"{cand.source}:{cand.url_or_path[:80]} → {type(exc).__name__}: {exc}"
            )

    detail = "; ".join([f"{k}: {v}" for k, v in errors.items()] + fetch_errors) \
        or "no source produced candidates"
    raise FulltextError(
        f"fulltext unavailable for doi={doi!r} title={title!r}. "
        f"Per-source reasons: {detail}"
    )
