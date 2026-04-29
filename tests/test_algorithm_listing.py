"""Algorithm listing 处理与 pgf 清洗的单测。"""

from __future__ import annotations

from bs4 import BeautifulSoup

from qiq_html2md.stages.enrich import (
    _find_tex_annotation,
    _is_pgf_polluted,
    _line_is_pgf_noise,
    _process_algorithm_listings,
)


def test_is_pgf_polluted_detects_markers() -> None:
    assert _is_pgf_polluted(r"q_{\text{search}}\leftarrow\pgfsys@beginscope")
    assert _is_pgf_polluted(r"\hbox to116pt{\pgfpicture...}")
    assert _is_pgf_polluted(r"\leavevmode\hbox to0.0pt")
    # 正常 LaTeX 不应命中
    assert not _is_pgf_polluted(r"q_{\text{search}}")
    assert not _is_pgf_polluted(r"\sum_{i=0}^{N} a_i")
    assert not _is_pgf_polluted("")


def test_find_tex_annotation_rejects_pgf_polluted_alttext() -> None:
    html = r"""
    <math alttext="q\leftarrow\pgfsys@rect{-53pt}{-4pt}">
        <mi>q</mi>
    </math>
    """
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("math")
    result = _find_tex_annotation(tag)  # type: ignore[arg-type]
    assert result is None, "pgf 污染的 alttext 应被弃用"


def test_find_tex_annotation_rejects_pgf_polluted_annotation() -> None:
    html = r"""
    <math>
        <semantics>
            <mi>q</mi>
            <annotation encoding="application/x-tex">q\leftarrow\pgfpicture\makeatletter</annotation>
        </semantics>
    </math>
    """
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("math")
    result = _find_tex_annotation(tag)  # type: ignore[arg-type]
    assert result is None


def test_find_tex_annotation_accepts_clean_latex() -> None:
    html = r'<math alttext="\sum_{i=0}^{N} x_i"><mi>x</mi></math>'
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("math")
    result = _find_tex_annotation(tag)  # type: ignore[arg-type]
    assert result == r"\sum_{i=0}^{N} x_i"


def test_line_is_pgf_noise() -> None:
    assert _line_is_pgf_noise(r"\pgfsys@rect{-53pt}{-4pt}")
    assert _line_is_pgf_noise(r"  \definecolor{named}{rgb}{0.0,0.2,0.5}")
    assert _line_is_pgf_noise(r"\pgf@stroke")
    # 纯符号行
    assert _line_is_pgf_noise("{{}{}}}{")
    # 正常代码行不应命中
    assert not _line_is_pgf_noise("Extract search query: q")
    assert not _line_is_pgf_noise("if condition then return answer")
    assert not _line_is_pgf_noise("")


def test_process_algorithm_listings_figure() -> None:
    """LaTeXML 风格 figure.ltx_float_algorithm 应被整体替换为占位节点。"""
    html = """
    <body>
      <p>Before algorithm.</p>
      <figure class="ltx_float ltx_float_algorithm" id="alg1">
        <figcaption class="ltx_caption">Algorithm 1 Search Loop</figcaption>
        <div class="ltx_listing" id="alg1.3">
          <div class="ltx_listingline">1: Input: <math alttext="Q">Q</math></div>
          <div class="ltx_listingline">2: for each step do</div>
          <div class="ltx_listingline">3:   Extract query</div>
          <div class="ltx_listingline">4: end for</div>
        </div>
      </figure>
      <p>After algorithm.</p>
    </body>
    """
    soup = BeautifulSoup(html, "lxml")
    algos, warnings = _process_algorithm_listings(soup)
    assert len(algos) == 1
    algo = algos[0]
    assert algo["id"] == "alg001"
    assert "Algorithm 1" in (algo["title"] or "")
    assert len(algo["lines"]) == 4
    assert "Input" in algo["lines"][0]
    assert "end for" in algo["lines"][3]
    # figure 节点应被占位符替换
    assert soup.find("figure") is None
    ph = soup.find(attrs={"data-h2m-id": "alg001"})
    assert ph is not None
    assert ph.get("data-h2m-kind") == "algorithm"


def test_process_algorithm_listings_strips_pgf_noise() -> None:
    """内嵌 <math alttext="...\\pgfsys..."> 的 listingline 应被清洗干净。"""
    html = r"""
    <body>
      <figure class="ltx_float_algorithm">
        <figcaption>Algorithm 1</figcaption>
        <div class="ltx_listing">
          <div class="ltx_listingline">
            1: Extract query <math alttext="q\leftarrow\pgfsys@rect{-53pt}{-4pt}\pgfsys@stroke">
              <mrow><mi>q</mi></mrow>
            </math>
          </div>
          <div class="ltx_listingline">
            2: Output result
          </div>
        </div>
      </figure>
    </body>
    """
    soup = BeautifulSoup(html, "lxml")
    algos, _warnings = _process_algorithm_listings(soup)
    assert len(algos) == 1
    # 行里不应有 pgfsys 等 TikZ 噪声
    for line in algos[0]["lines"]:
        assert "pgfsys" not in line.lower()
        assert "pgfpicture" not in line.lower()
    # 应保留可读正文
    joined = "\n".join(algos[0]["lines"])
    assert "Extract query" in joined
    assert "Output result" in joined


def test_process_algorithm_listings_standalone_div() -> None:
    """没有 figure 包裹的独立 div.ltx_listing 也应识别。"""
    html = """
    <body>
      <div class="ltx_listing">
        <div class="ltx_listingline">foo bar</div>
        <div class="ltx_listingline">baz qux</div>
      </div>
    </body>
    """
    soup = BeautifulSoup(html, "lxml")
    algos, _warnings = _process_algorithm_listings(soup)
    assert len(algos) == 1
    assert algos[0]["title"] is None
    assert algos[0]["lines"] == ["foo bar", "baz qux"]
