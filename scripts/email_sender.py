"""
Step 6. 섹터 애널리스트 이메일 발송
- Gmail SMTP_SSL (옛 send_sector_mail 패턴 재활용)
- 한 애널리스트 = 1통 (여러 섹터 묶음)
- test_recipient 인자가 있으면 모든 메일을 그쪽으로 (제목에 [TEST] prefix, 원래 수신자 정보 본문 표시)
- 메일 내용: 담당 섹터별 AI 요약 + 핵심 뉴스 3 + US movers + 종목 헤드라인 한 줄씩 + 대시보드 링크
- 종목별 상세는 대시보드에서 보도록
"""

import os, json, smtplib, ast
from email.message import EmailMessage
from datetime import datetime
from pathlib import Path

import pandas as pd


# ============================================================
# 1. 애널리스트별 담당 WICS 섹터 그룹화
# - main_combined_stock에서 종목 → 담당자 → WICS 매핑을 역산
# - 한 애널리스트가 담당 종목들의 WICS 3rd-level을 모두 모음
# ============================================================

def group_sectors_by_analyst(analyst_df: pd.DataFrame,
                              main_combined_stock: pd.DataFrame,
                              wics_slug_map: dict,
                              coverage_sector_list: list[str],
                              email_col: str = "메일",
                              name_col: str = "담당자",
                              wics_col: str = "WICS분류") -> dict[str, dict]:
    """
    한 애널리스트가 담당하는 종목들의 WICS 3rd-level을 그룹화.

    Args:
        analyst_df: 담당자 ↔ 메일 매핑
        main_combined_stock: 종목 ↔ 담당자 ↔ WICS분류 (커버 종목만)
        wics_slug_map: {WICS분류 풀네임: slug}
        coverage_sector_list: 45개 커버 WICS 3rd-level
        wics_col: main_combined_stock에서 WICS 분류 컬럼명

    Returns: {
        "홍길동": {
            "name":    "홍길동",
            "email":   "hong@...",
            "sectors": ["조선", "건설"],          # WICS 3rd-level 한글
            "wics_full": ["산업재-자본재-조선", ...],  # 풀네임
            "slugs":   ["shipbuilding", ...],
        }
    }
    """
    # 담당자 → 메일 lookup
    name_to_email = {}
    for _, row in analyst_df.iterrows():
        name = row.get(name_col)
        email = row.get(email_col)
        if name and email:
            name_to_email[name] = email

    # 담당자 → WICS 풀네임 set
    name_to_wics: dict[str, set] = {}
    for _, row in main_combined_stock.iterrows():
        name = row.get(name_col)
        wics = row.get(wics_col)
        if not name or not wics:
            continue
        # coverage_sector_list 안에 있는 것만 채택
        if wics not in coverage_sector_list:
            continue
        name_to_wics.setdefault(name, set()).add(wics)

    out: dict[str, dict] = {}
    for name, wics_set in name_to_wics.items():
        email = name_to_email.get(name)
        if not email:
            continue
        wics_list = sorted(wics_set)
        slugs = [wics_slug_map[w] for w in wics_list if w in wics_slug_map]
        sectors_3rd = [w.split("-")[-1] for w in wics_list]
        out[name] = {
            "name":      name,
            "email":     email,
            "sectors":   sectors_3rd,
            "wics_full": wics_list,
            "slugs":     slugs,
        }
    return out


# ============================================================
# 2. 메일 HTML 빌더
# ============================================================

CSS = """
body { font-family: -apple-system, 'Segoe UI', sans-serif; color: #111; background: #fafaf7;
       margin: 0; padding: 24px 0; line-height: 1.6; }
.wrap { max-width: 720px; margin: 0 auto; background: #fafaf7; border: 1px solid #e8e6e0; border-radius: 6px; overflow: hidden; padding-bottom: 4px; }
.header { padding: 24px 28px; border-bottom: 1px solid #e8e6e0; background: #fff; }
.header-brand { font-size: 11px; font-weight: 700; letter-spacing: 0.06em; color: #888; text-transform: uppercase; margin-bottom: 4px; }
.header h1 { font-size: 22px; font-weight: 700; margin: 0; letter-spacing: -0.02em; }
.header .meta { font-size: 12px; color: #888; margin-top: 4px; }
.test-banner { background: #fbeae7; border-bottom: 1px solid #c0392b; padding: 10px 28px; color: #c0392b; font-size: 12px; font-weight: 600; }
.section { padding: 24px 28px; }
.sector-block {
  padding: 0;
  margin: 24px 24px 0 24px;
  border: 2px solid #111;
  border-radius: 6px;
  overflow: hidden;
  background: #fff;
}
.sector-block:first-of-type { margin-top: 24px; }
.sector-header {
  background: #111;
  color: #fff;
  padding: 14px 22px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.sector-header .sector-name {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.sector-header .sector-path {
  font-size: 11px;
  color: rgba(255,255,255,0.7);
  letter-spacing: 0.04em;
}
.sector-body { padding: 20px 22px 22px 22px; }
.summary-box { background: #fff; border: 1px solid #e8e6e0; border-left: 4px solid #111; padding: 16px 20px; margin-bottom: 18px; border-radius: 4px; }
.summary-box .label { font-size: 10px; font-weight: 700; letter-spacing: 0.1em;
                       background: #111; color: #fff; padding: 2px 6px; border-radius: 3px; display: inline-block; margin-bottom: 10px; }
.summary-box ul { list-style: none; padding: 0; margin: 0; }
.summary-box li { font-size: 14px; padding: 4px 0 4px 16px; position: relative; line-height: 1.5; }
.summary-box li::before { content: '·'; position: absolute; left: 4px; font-weight: 900; }
.h-sub { font-size: 13px; font-weight: 700; margin: 16px 0 8px 0; color: #111; padding-bottom: 6px; border-bottom: 1px dashed #e8e6e0; }
.news-li { padding: 8px 0; border-bottom: 1px solid #f5f3ee; font-size: 13px; }
.news-li:last-child { border-bottom: none; }
.news-meta { font-size: 11px; color: #888; margin-bottom: 2px; }
.tag { display: inline-block; padding: 1px 6px; border-radius: 2px; font-size: 10px; font-weight: 600; margin-right: 4px; }
.tag-positive { background: #e3f1e6; color: #1e7e34; }
.tag-negative { background: #fbeae7; color: #c0392b; }
.tag-neutral { background: #efefef; color: #555; }
.news-title { font-weight: 600; color: #111; text-decoration: none; }
.news-title:hover { text-decoration: underline; }
.us-mover { padding: 8px 0; font-size: 12px; border-bottom: 1px solid #f5f3ee; }
.us-mover:last-child { border-bottom: none; }
.ticker { font-family: 'SF Mono', monospace; font-weight: 700; background: #111; color: #fff; padding: 1px 6px; border-radius: 2px; font-size: 10px; }
.change-up   { color: #c0392b; font-weight: 700; font-family: 'SF Mono', monospace; }
.change-down { color: #1565c0; font-weight: 700; font-family: 'SF Mono', monospace; }
.stock-line { font-size: 12px; padding: 6px 0; color: #444; border-bottom: 1px dashed #f0eee8; }
.stock-line:last-child { border-bottom: none; }
.stock-line .stock-name { font-weight: 700; color: #111; }
.stock-line .stock-code { font-family: 'SF Mono', monospace; font-size: 10px; color: #888; margin-left: 4px; }
.cta-wrap { padding: 18px 28px 24px; text-align: center; background: #fafaf7; border-top: 1px solid #e8e6e0; }
.cta { display: inline-block; background: #111; color: #fff !important; padding: 10px 20px; border-radius: 4px;
       text-decoration: none; font-weight: 700; font-size: 13px; letter-spacing: -0.01em; }
.footer { padding: 16px 28px; border-top: 1px solid #e8e6e0; font-size: 11px; color: #888; text-align: center; }
.empty { font-size: 12px; color: #888; padding: 8px 0; font-style: italic; }
"""


def _sentiment_tag(s: str) -> str:
    if s == "positive": return '<span class="tag tag-positive">긍정</span>'
    if s == "negative": return '<span class="tag tag-negative">부정</span>'
    return '<span class="tag tag-neutral">중립</span>'


def _fmt_pct(p) -> str:
    if p is None: return ""
    cls = "change-up" if p >= 0 else "change-down"
    sign = "+" if p >= 0 else ""
    return f'<span class="{cls}">{sign}{p:.2f}%</span>'


def _esc(s) -> str:
    if s is None: return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _format_date_short(iso: str) -> str:
    if not iso: return ""
    return iso[5:10].replace("-", ".")


def _build_sector_block(sector_data: dict, slug: str, pages_base_url: str) -> str:
    """한 섹터 분량의 HTML 블록."""
    wics_full = f"{sector_data.get('wics_1st','')} › {sector_data.get('wics_2nd','')} › {sector_data.get('wics_3rd','')}"

    # AI 요약
    bullets = sector_data.get("ai_summary") or []
    summary_html = ""
    if bullets:
        items = "".join(f"<li>{_esc(b)}</li>" for b in bullets)
        summary_html = f'<div class="summary-box"><span class="label">AI 요약</span><ul>{items}</ul></div>'

    # 섹터 핵심 뉴스 (상위 3건)
    news = (sector_data.get("news") or [])[:3]
    news_html = ""
    if news:
        items = []
        for n in news:
            items.append(f"""
              <li class="news-li">
                <div class="news-meta">
                  {_sentiment_tag(n.get('sentiment','neutral'))}
                  {_format_date_short(n.get('published_at',''))} · {_esc(n.get('source',''))}
                </div>
                <a href="{_esc(n.get('url',''))}" class="news-title">{_esc(n.get('title',''))}</a>
              </li>
            """)
        news_html = f'<div class="h-sub">📰 섹터 핵심 이슈</div><ul style="list-style:none;padding:0;margin:0;">{"".join(items)}</ul>'

    # 미국 시장 동향
    us_movers = sector_data.get("us_movers") or []
    us_html = ""
    if us_movers:
        items = []
        for m in us_movers[:5]:
            items.append(f"""
              <div class="us-mover">
                <span class="ticker">{_esc(m.get('ticker',''))}-US</span>
                <strong>{_esc(m.get('name',''))}</strong>
                ${_esc(f"{m.get('price','-'):.2f}" if m.get('price') is not None else '-')}
                {_fmt_pct(m.get('change_pct'))}
                <div style="color:#444;margin-top:2px;font-size:12px;">{_esc(m.get('reason',''))}</div>
              </div>
            """)
        us_html = f'<div class="h-sub">🌐 미국 시장 동향</div>{"".join(items)}'

    # 종목 헤드라인 (신규 이슈 있는 종목만, 한 줄씩)
    stocks = sector_data.get("stocks") or []
    stock_lines = []
    for s in stocks:
        # 신규 카운트
        new_news = len([n for n in (s.get("news") or []) if n.get("is_new")])
        new_disc = len([d for d in (s.get("disclosures") or []) if d.get("is_new")])
        new_repo = len([r for r in (s.get("reports") or []) if r.get("is_new")])
        new_cons_y = len([c for c in (s.get("consensus", {}).get("Y") or []) if c.get("is_new")])
        new_cons_q = len([c for c in (s.get("consensus", {}).get("Q") or []) if c.get("is_new")])
        new_cons = new_cons_y + new_cons_q
        total = new_news + new_disc + new_repo + new_cons
        if total == 0:
            continue
        change_html = _fmt_pct(s.get("change_pct"))
        bits = []
        if new_news: bits.append(f"뉴스 {new_news}")
        if new_disc: bits.append(f"공시 {new_disc}")
        if new_cons: bits.append(f"컨센 {new_cons}")
        if new_repo: bits.append(f"리포트 {new_repo}")

        stock_lines.append(f"""
          <div class="stock-line">
            <span class="stock-name">{_esc(s.get('name',''))}</span>
            <span class="stock-code">{_esc(s.get('code',''))}</span>
            {change_html}
            &nbsp;<span style="color:#888">·</span>&nbsp;
            {' · '.join(bits)}
          </div>
        """)
    stocks_html = ""
    if stock_lines:
        stocks_html = f'<div class="h-sub">📊 신규 이슈 발생 종목 ({len(stock_lines)}개)</div>{"".join(stock_lines)}'

    # 빈 섹터 메시지
    if not (summary_html or news_html or us_html or stocks_html):
        body = '<div class="empty">신규 이슈 없음.</div>'
    else:
        body = summary_html + news_html + us_html + stocks_html

    sector_url = f"{pages_base_url.rstrip('/')}/sectors/{slug}.html"
    return f"""
      <div class="sector-block">
        <div class="sector-header">
          <span class="sector-name">{_esc(sector_data.get('wics_3rd',''))}</span>
          <span class="sector-path">{_esc(wics_full)}</span>
        </div>
        <div class="sector-body">
          {body}
          <div style="margin-top:18px;text-align:right;">
            <a href="{_esc(sector_url)}" style="color:#111;font-size:12px;font-weight:600;text-decoration:underline;">
              대시보드에서 종목별 상세 보기 →
            </a>
          </div>
        </div>
      </div>
    """


def build_email_html(analyst_name: str,
                     sector_data_map: dict[str, dict],   # {slug: sector_json_dict}
                     trading_date: str,
                     pages_base_url: str,
                     test_recipient: str | None = None,
                     real_recipient: str | None = None) -> str:
    sector_blocks = "\n".join(
        _build_sector_block(data, slug, pages_base_url)
        for slug, data in sector_data_map.items()
    )

    test_banner = ""
    if test_recipient:
        test_banner = f"""
          <div class="test-banner">
            ⚠️ TEST 모드 — 실제 수신자({_esc(real_recipient or '미정')}) 대신 {_esc(test_recipient)}로 발송됨
          </div>
        """

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><style>{CSS}</style></head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="header-brand">KIS · 섹터별 이슈 브리핑</div>
      <h1>{_esc(analyst_name)} 담당 섹터 데일리 브리핑</h1>
      <div class="meta">{_esc(trading_date)} · 자산관리전략부 디지털리서치팀</div>
    </div>
    {test_banner}
    {sector_blocks}
    <div class="cta-wrap">
      <a href="{_esc(pages_base_url)}" class="cta">📊 전체 대시보드 보기</a>
    </div>
    <div class="footer">© 한국투자증권 자산관리전략부 디지털리서치팀 · 내부용</div>
  </div>
</body></html>
"""


# ============================================================
# 3. 발송
# ============================================================

def _send_via_gmail(smtp_user: str, smtp_pass: str,
                    to_addresses: list[str], subject: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = ", ".join(to_addresses)
    msg["Subject"] = subject
    msg.set_content("이 메일은 HTML 지원 클라이언트에서만 정상 표시됩니다.", subtype="plain")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)


def send_sector_emails(*,
                       repo_root: str,
                       trading_date: str,
                       analyst_df: pd.DataFrame,
                       main_combined_stock: pd.DataFrame,
                       coverage_sector_list: list[str],
                       wics_slug_map: dict,
                       pages_base_url: str,
                       smtp_user: str,
                       smtp_pass: str,
                       wics_col: str = "WICS분류",
                       test_recipient: str | None = None,
                       dry_run: bool = False,
                       output_dir: str | None = None,
                       verbose: bool = True) -> None:
    """
    한 애널리스트 = 1통 발송 (담당 섹터들을 묶음).

    test_recipient: 지정 시 모든 메일이 이 주소로만 감 (제목에 [TEST] prefix).
    dry_run: True면 발송 없이 HTML 파일로 저장 (output_dir 또는 ./output_mails).
    wics_col: main_combined_stock 안의 WICS 분류 컬럼명 (기본 "WICS분류").
    """
    sectors_dir = os.path.join(repo_root, 'data', 'sectors')

    # 1) 애널리스트별 담당 WICS 섹터 그룹화 (combined_stock에서 역산)
    grouped = group_sectors_by_analyst(
        analyst_df=analyst_df,
        main_combined_stock=main_combined_stock,
        wics_slug_map=wics_slug_map,
        coverage_sector_list=coverage_sector_list,
        wics_col=wics_col,
    )
    if verbose:
        print(f"[Email] 애널리스트 {len(grouped)}명")

    # dry_run 출력 폴더
    if dry_run:
        out = Path(output_dir or "./output_mails")
        out.mkdir(parents=True, exist_ok=True)

    sent, failed, skipped = 0, 0, 0
    for analyst_name, info in grouped.items():
        # 섹터 JSON 모으기
        sector_data_map = {}
        for slug in info['slugs']:
            path = os.path.join(sectors_dir, f"{slug}.json")
            if not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8') as f:
                sector_data_map[slug] = json.load(f)

        if not sector_data_map:
            if verbose: print(f"  [SKIP] {analyst_name} → 섹터 데이터 없음")
            skipped += 1
            continue

        # 메일 본문
        html_body = build_email_html(
            analyst_name=analyst_name,
            sector_data_map=sector_data_map,
            trading_date=trading_date,
            pages_base_url=pages_base_url,
            test_recipient=test_recipient,
            real_recipient=info['email'],
        )

        # 수신자 / 제목
        sectors_label = ", ".join(info['sectors'])
        if test_recipient:
            to_addresses = [test_recipient]
            subject = f"[TEST][데일리이슈] {analyst_name} ({sectors_label}) | {trading_date}"
        else:
            to_addresses = [info['email']]
            subject = f"[데일리이슈] {sectors_label} | {trading_date}"

        # dry_run 또는 실제 발송
        if dry_run:
            safe = analyst_name.replace("/", "_")
            path = out / f"{trading_date}_{safe}.html"
            path.write_text(html_body, encoding="utf-8")
            if verbose: print(f"  [DRY] {analyst_name:12s} → {path}")
            sent += 1
            continue

        try:
            _send_via_gmail(smtp_user, smtp_pass, to_addresses, subject, html_body)
            if verbose:
                print(f"  [SENT] {analyst_name:12s} → {to_addresses[0]} "
                      f"({len(sector_data_map)}섹터)")
            sent += 1
        except Exception as e:
            if verbose:
                print(f"  [ERR ] {analyst_name:12s} → {e}")
            failed += 1

    if verbose:
        print(f"[Email] 완료 — 발송 {sent}, 실패 {failed}, 스킵 {skipped}")
