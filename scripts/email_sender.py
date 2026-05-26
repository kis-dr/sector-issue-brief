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
                              wics_col: str = "WICS분류",
                              full_combined_stock: pd.DataFrame | None = None) -> dict[str, dict]:
    """
    한 애널리스트가 담당하는 종목들의 WICS 3rd-level을 그룹화.
    full_combined_stock: 정+부 모두 포함된 combined_stock (부담당자 WICS 매핑용)
    """
    # 담당자 → 메일 lookup
    name_to_email = {}
    for _, row in analyst_df.iterrows():
        name = row.get(name_col)
        email = row.get(email_col)
        if name and email and pd.notna(name) and pd.notna(email):
            name_to_email[str(name).strip()] = str(email).strip()

    # 담당자 → WICS set
    # 정담당: main_combined_stock (역할=='정')
    # 부담당: full_combined_stock 중 역할=='부' (있으면)
    stock_df = full_combined_stock if full_combined_stock is not None else main_combined_stock

    name_to_wics: dict[str, set] = {}
    for _, row in stock_df.iterrows():
        wics = row.get(wics_col)
        if not wics or wics not in coverage_sector_list:
            continue
        name = row.get('담당자')
        if name and pd.notna(name) and str(name).strip():
            name_to_wics.setdefault(str(name).strip(), set()).add(wics)

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

CSS = ""  # 인라인 스타일 사용 (Outlook 호환)


def _sentiment_tag(s: str) -> str:
    """Outlook 호환: inline style 사용"""
    if s == "positive":
        bg, fg, txt = "#e3f1e6", "#1e7e34", "긍정"
    elif s == "negative":
        bg, fg, txt = "#fbeae7", "#c0392b", "부정"
    else:
        bg, fg, txt = "#efefef", "#555", "중립"
    return f'<span style="display:inline-block;padding:1px 6px;border-radius:2px;font-size:10px;font-weight:600;background:{bg};color:{fg};margin-right:4px;">{txt}</span>'


def _fmt_pct(p) -> str:
    if p is None:
        return ""
    color = "#c0392b" if p >= 0 else "#1565c0"
    sign = "+" if p >= 0 else ""
    return f'<span style="color:{color};font-weight:700;font-family:Consolas,monospace;">{sign}{p:.2f}%</span>'


def _fmt_krw(val) -> str:
    """원 단위 → '1조 2,345억' / '345억' / '0.12억' 형식"""
    if val is None:
        return "-"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "-"
    if v == 0:
        return "0"
    neg = v < 0
    abs_v = abs(v)
    if abs_v >= 1_000_000_000_000:
        jo = int(abs_v // 1_000_000_000_000)
        eok = round((abs_v % 1_000_000_000_000) / 100_000_000)
        txt = f"{jo:,}조 {eok:,}억" if eok > 0 else f"{jo:,}조"
    elif abs_v >= 100_000_000:
        eok = round(abs_v / 100_000_000)
        txt = f"{eok:,}억"
    else:
        txt = f"{abs_v / 100_000_000:.2f}억"
    return f"-{txt}" if neg else txt


def _fmt_market_cap(cap) -> str:
    """시가총액 — 단위표시 없이 숫자만 (회색)"""
    if cap is None:
        return ""
    try:
        v = float(cap)
        if v == 0:
            return ""
    except (TypeError, ValueError):
        return ""
    return _fmt_krw(v) + "원"


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _format_date_short(iso: str) -> str:
    if not iso:
        return ""
    return iso[5:10].replace("-", ".")


def _build_sector_block(sector_data: dict, slug: str, pages_base_url: str) -> str:
    """한 섹터 분량의 HTML — Outlook-호환 (inline style + table)."""
    wics_full = f"{sector_data.get('wics_1st','')} › {sector_data.get('wics_2nd','')} › {sector_data.get('wics_3rd','')}"

    # ─── AI 요약 ───
    bullets = sector_data.get("ai_summary") or []
    if bullets:
        lis = "".join(
            f'<li style="font-size:14px;padding:3px 0 3px 4px;line-height:1.5;color:#111;">{_esc(b)}</li>'
            for b in bullets
        )
        summary_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:#fff;border-left:4px solid #111;margin-bottom:18px;">
          <tr><td style="padding:14px 18px;">
            <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;background:#111;color:#fff;padding:2px 6px;display:inline-block;margin-bottom:8px;">AI 요약</div>
            <ul style="list-style:disc;padding-left:18px;margin:0;">{lis}</ul>
          </td></tr>
        </table>
        """
    else:
        summary_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="background:#fff;border-left:4px solid #111;margin-bottom:18px;">
          <tr><td style="padding:14px 18px;">
            <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;background:#111;color:#fff;padding:2px 6px;display:inline-block;margin-bottom:8px;">AI 요약</div>
            <div style="font-size:12px;color:#999;font-style:italic;">요약 없음</div>
          </td></tr>
        </table>
        """

    # ─── 섹터 핵심 뉴스 (상위 3건) ───
    news = (sector_data.get("news") or [])[:3]
    h_sub_style = ('font-size:13px;font-weight:700;margin:18px 0 8px 0;color:#111;'
                   'padding-bottom:6px;border-bottom:1px dashed #e8e6e0;')
    if news:
        rows = []
        for n in news:
            kw_tag = (f'<span style="display:inline-block;padding:1px 6px;border-radius:2px;'
                      f'font-size:10px;font-weight:600;background:#e8e8ff;color:#4040b0;'
                      f'margin-right:4px;">#{_esc(n.get("keyword",""))}</span>'
                      if n.get("keyword") else "")
            title_html = (f'<a href="{_esc(n.get("url",""))}" style="color:#111;text-decoration:none;font-weight:600;">{_esc(n.get("title",""))}</a>'
                          if n.get("url")
                          else f'<span style="color:#111;font-weight:600;">{_esc(n.get("title",""))}</span>')
            rows.append(f"""
              <tr><td style="padding:8px 0;border-bottom:1px solid #f5f3ee;font-size:13px;">
                <div style="font-size:11px;color:#888;margin-bottom:3px;">
                  {_sentiment_tag(n.get('sentiment','neutral'))}{kw_tag}
                  {_format_date_short(n.get('published_at',''))} · {_esc(n.get('source',''))}
                </div>
                {title_html}
              </td></tr>
            """)
        news_html = (f'<div style="{h_sub_style}">📰 섹터 핵심 뉴스</div>'
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{"".join(rows)}</table>')
    else:
        news_html = (f'<div style="{h_sub_style}">📰 섹터 핵심 뉴스</div>'
                     f'<div style="font-size:12px;color:#999;font-style:italic;padding:8px 0;">오늘 보고할 핵심 뉴스 없음</div>')

    # ─── US peer 동향 ───
    us_movers = sector_data.get("us_movers") or []
    if us_movers:
        items = []
        for m in us_movers[:5]:
            news_links_html = ""
            for nu in (m.get("news_urls") or [])[:2]:
                if nu.get("url"):
                    news_links_html += (f'<div style="font-size:11px;margin-top:3px;">'
                                         f'<a href="{_esc(nu["url"])}" style="color:#1565c0;text-decoration:none;">'
                                         f'📰 {_esc((nu.get("title") or "관련 뉴스")[:55])}</a></div>')
            price = m.get('price')
            price_str = f"${price:.2f}" if price is not None else "-"
            reason_val = m.get('reason', '')
            reason_html = ''
            if reason_val and reason_val != '사유 미상':
                reason_html = (f'<div style="color:#111;margin-top:4px;font-size:12px;line-height:1.5;'
                               f'padding:5px 10px;background:#fffbe6;border-left:3px solid #f5c542;'
                               f'border-radius:2px;font-weight:600;">변동사유: {_esc(reason_val)}</div>')
            items.append(f"""
              <tr><td style="padding:8px 0;border-bottom:1px solid #f5f3ee;font-size:12px;">
                <span style="font-family:Consolas,monospace;font-weight:700;background:#111;color:#fff;padding:1px 6px;border-radius:2px;font-size:10px;">{_esc(m.get('ticker',''))}</span>
                <strong style="margin-left:6px;">{_esc(m.get('name',''))}</strong>
                <span style="margin-left:6px;color:#444;">{price_str}</span>
                <span style="margin-left:6px;">{_fmt_pct(m.get('change_pct'))}</span>
                {reason_html}
                {news_links_html}
              </td></tr>
            """)
        us_html = (f'<div style="{h_sub_style}">🌐 US peer 동향</div>'
                   f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{"".join(items)}</table>')
    else:
        us_html = (f'<div style="{h_sub_style}">🌐 US peer 동향</div>'
                   f'<div style="font-size:12px;color:#999;font-style:italic;padding:8px 0;">관련 변동 종목 없음</div>')

    # ─── 종목 전체 나열 (현재가 + 등락률 + 신규 이슈 상세) ───
    stocks = sector_data.get("stocks") or []
    stock_rows = []
    for s in stocks:
        change_html = _fmt_pct(s.get("change_pct"))
        price = s.get("price")
        price_html = f'<span style="font-family:Consolas,monospace;font-size:11px;color:#444;margin-left:4px;">{price:,.0f}원</span>' if price else ''

        # 브리핑 (간략 1줄 요약)
        brief = s.get('briefing') or {}
        brief_text = brief.get('briefing', '')
        brief_url = brief.get('content_url', '')
        detail_html = ''
        if brief_text:
            # 핵심만 추출: 최대 60자
            short = brief_text[:60].rstrip('.')
            if len(brief_text) > 60:
                short += '…'
            if brief_url:
                detail_html = (f'<div style="font-size:11px;color:#444;margin-top:3px;">'
                               f'💬 <a href="{_esc(brief_url)}" style="color:#1565c0;text-decoration:none;">'
                               f'{_esc(short)}</a></div>')
            else:
                detail_html = f'<div style="font-size:11px;color:#444;margin-top:3px;">💬 {_esc(short)}</div>'

        stock_rows.append(f"""
          <tr><td style="padding:7px 0;border-bottom:1px dashed #f0eee8;font-size:12px;{'border-left:3px solid #c0392b;padding-left:8px;' if s.get('has_new') else ''}">
            {'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#c0392b;margin-right:5px;vertical-align:middle;"></span>' if s.get('has_new') else ''}
            <strong style="color:#111;">{_esc(s.get('name',''))}</strong>
            <span style="font-family:Consolas,monospace;font-size:10px;color:#888;margin-left:4px;">{_esc(s.get('code',''))}</span>
            {price_html}
            <span style="margin-left:6px;">{change_html}</span>
            {detail_html}
          </td></tr>
        """)

    if stock_rows:
        stocks_html = (f'<div style="{h_sub_style}">📊 종목 현황 ({len(stock_rows)}개)</div>'
                       f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{"".join(stock_rows)}</table>')
    else:
        stocks_html = (f'<div style="{h_sub_style}">📊 종목 현황</div>'
                       f'<div style="font-size:12px;color:#999;font-style:italic;padding:8px 0;">커버 종목 없음</div>')

    # ─── 독점 글로벌 리서치 (KIS 글로벌 리서치) ───
    gr_items = sector_data.get("global_research") or []
    if gr_items:
        rows = []
        for r in gr_items[:5]:
            cat_tag = (f'<span style="display:inline-block;padding:1px 6px;border-radius:2px;'
                       f'font-size:10px;font-weight:600;background:#fef3e0;color:#b87a00;'
                       f'margin-right:4px;">{_esc(r.get("category",""))}</span>'
                       if r.get("category") else "")
            title_html = (f'<a href="{_esc(r.get("url",""))}" style="color:#111;text-decoration:none;font-weight:600;">{_esc(r.get("title",""))}</a>'
                          if r.get("url")
                          else f'<span style="color:#111;font-weight:600;">{_esc(r.get("title",""))}</span>')
            rows.append(f"""
              <tr><td style="padding:8px 0;border-bottom:1px solid #f5f3ee;font-size:13px;">
                <div style="font-size:11px;color:#888;margin-bottom:3px;">
                  {cat_tag}
                  {_format_date_short(r.get('published_at',''))} · {_esc(r.get('publisher',''))}
                </div>
                {title_html}
              </td></tr>
            """)
        gr_html = (f'<div style="{h_sub_style}">📚 독점 글로벌 리서치</div>'
                   f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{"".join(rows)}</table>')
    else:
        gr_html = (f'<div style="{h_sub_style}">📚 독점 글로벌 리서치</div>'
                   f'<div style="font-size:12px;color:#999;font-style:italic;padding:8px 0;">오늘 발행된 리서치 없음</div>')

    body = summary_html + news_html + gr_html + us_html + stocks_html

    sec_return = sector_data.get('sector_return')
    sec_return_html = ''
    if sec_return is not None:
        color = "#ff6b6b" if sec_return >= 0 else "#74b9ff"
        sign = "+" if sec_return >= 0 else ""
        sec_return_html = (f'<div style="font-size:13px;color:{color};font-weight:700;'
                            f'font-family:Consolas,monospace;margin-top:4px;">'
                            f'{sign}{sec_return:.2f}%</div>')

    sector_url = f"{pages_base_url.rstrip('/')}/sectors/{slug}.html"
    return f"""
      <!--[if mso]><table role="presentation" width="672" align="center" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="max-width:672px;margin:24px auto 0 auto;background:#fff;border:2px solid #111;border-radius:6px;">
        <tr>
          <td style="background:#111;color:#fff;padding:14px 22px;">
            <div style="font-size:18px;font-weight:700;color:#fff;line-height:1.2;">{_esc(sector_data.get('wics_3rd',''))}</div>
            <div style="font-size:11px;color:#bbb;margin-top:3px;">{_esc(wics_full)}</div>
            {sec_return_html}
          </td>
        </tr>
        <tr>
          <td style="padding:20px 22px 22px 22px;">
            {body}
            <div style="margin-top:18px;text-align:right;">
              <a href="{_esc(sector_url)}" style="color:#111;font-size:12px;font-weight:600;text-decoration:underline;">
                대시보드에서 종목별 상세 보기 →
              </a>
            </div>
          </td>
        </tr>
      </table>
      <!--[if mso]></td></tr></table><![endif]-->
      <!-- spacer -->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td height="20" style="font-size:0;line-height:0;">&nbsp;</td></tr></table>
    """


def build_email_html(analyst_name: str,
                     sector_data_map: dict[str, dict],   # {slug: sector_json_dict}
                     trading_date: str,
                     display_date: str,   
                     pages_base_url: str,
                     test_recipient: str | None = None,
                     real_recipient: str | None = None) -> str:
    """Outlook-호환 HTML (inline style + table 레이아웃)"""
    # 종목 수 내림차순 정렬
    sorted_items = sorted(sector_data_map.items(),
                          key=lambda x: len(x[1].get('stocks', [])), reverse=True)
    sector_blocks = "\n".join(
        _build_sector_block(data, slug, pages_base_url)
        for slug, data in sorted_items
    )

    test_banner = ""
    if test_recipient:
        test_banner = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td style="background:#fbeae7;border-bottom:1px solid #c0392b;padding:10px 28px;color:#c0392b;font-size:12px;font-weight:600;">
            ⚠️ TEST 모드 — 실제 수신자({_esc(real_recipient or '미정')}) 대신 {_esc(test_recipient)}로 발송됨
          </td></tr>
        </table>
        """

    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="ko">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <!--[if mso]>
  <xml>
    <o:OfficeDocumentSettings>
      <o:PixelsPerInch>96</o:PixelsPerInch>
    </o:OfficeDocumentSettings>
  </xml>
  <![endif]-->
  <title>섹터별 이슈 브리핑</title>
</head>
<body style="margin:0;padding:0;background:#fafaf7;font-family:'맑은 고딕','Malgun Gothic',Arial,sans-serif;color:#111;line-height:1.6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fafaf7;">
    <tr><td align="center" style="padding:24px 0;">
      <!--[if mso]><table role="presentation" width="720" align="center" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:720px;background:#fafaf7;">
        <tr><td>
          <!-- 헤더 -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fff;border:1px solid #e8e6e0;border-radius:6px;">
            <tr><td style="padding:24px 28px;border-bottom:1px solid #e8e6e0;">
              <div style="font-size:11px;font-weight:700;letter-spacing:0.06em;color:#888;text-transform:uppercase;margin-bottom:4px;">KIS · 섹터별 이슈 브리핑</div>
              <div style="font-size:22px;font-weight:700;color:#111;letter-spacing:-0.02em;">{_esc(analyst_name)} 담당 섹터 데일리 브리핑</div>
              <div style="font-size:12px;color:#888;margin-top:4px;">{_esc(display_date)} · 자산관리전략부 디지털리서치팀</div>
            </td></tr>
          </table>
          {test_banner}
          <!-- 섹터 블록들 -->
          {sector_blocks}
          <!-- CTA -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;background:#fafaf7;">
            <tr><td align="center" style="padding:18px 28px 24px 28px;">
              <a href="{_esc(pages_base_url)}" style="display:inline-block;background:#111;color:#fff;padding:11px 22px;border-radius:4px;text-decoration:none;font-weight:700;font-size:13px;">
                📊 전체 대시보드 보기
              </a>
            </td></tr>
          </table>
          <!-- 푸터 -->
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr><td align="center" style="padding:16px 28px;font-size:11px;color:#888;">
              © 한국투자증권 자산관리전략부 디지털리서치팀 · 내부용
            </td></tr>
          </table>
        </td></tr>
      </table>
      <!--[if mso]></td></tr></table><![endif]-->
    </td></tr>
  </table>
</body>
</html>
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
                       display_date: str,   
                       analyst_df: pd.DataFrame,
                       main_combined_stock: pd.DataFrame,
                       combined_stock: pd.DataFrame | None = None,
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
    sectors_dir = os.path.join(repo_root, 'data', 'sectors')

    # 1) 애널리스트별 담당 WICS 섹터 그룹화
    grouped = group_sectors_by_analyst(
        analyst_df=analyst_df,
        main_combined_stock=main_combined_stock,
        wics_slug_map=wics_slug_map,
        coverage_sector_list=coverage_sector_list,
        wics_col=wics_col,
        full_combined_stock=combined_stock,  # 정+부 모두
    )
    if verbose:
        print(f"[Email] 애널리스트 {len(grouped)}명")

    # dry_run 출력 폴더
    if dry_run:
        out = Path(output_dir or "./output_mails")
        out.mkdir(parents=True, exist_ok=True)

    sent, failed, skipped = 0, 0, 0
    for analyst_name, info in grouped.items():
        # 섹터 JSON 모으기 (날짜 suffix 적용)
        sector_data_map = {}
        for slug in info['slugs']:
            path = os.path.join(sectors_dir, f"{slug}_{trading_date}.json")
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
            display_date=display_date,   
            pages_base_url=pages_base_url,
            test_recipient=test_recipient,
            real_recipient=info['email'],
        )

        # 수신자 / 제목
        sectors_label = ", ".join(info['sectors'])
        if test_recipient:
            to_addresses = [test_recipient]
            subject = f"[TEST][데일리이슈] {analyst_name} ({sectors_label}) | {display_date}"
        else:
            to_addresses = [info['email']]
            subject = f"[데일리이슈] {sectors_label} | {display_date}"

        # dry_run 또는 실제 발송
        if dry_run:
            safe = analyst_name.replace("/", "_")
            path = out / f"{display_date}_{safe}.html"
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
