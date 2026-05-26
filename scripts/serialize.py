"""
Step 4. JSON 직렬화 모듈
- ipynb에서 build된 모든 데이터를 GitHub Pages용 JSON으로 변환
- AI 요약 (Gemini 3 bullet) 생성
- 종목별 가격 차트 다운로드 (yfinance → fdr fallback)
"""

import os, json, time, hashlib
import threading
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

# 외부 의존 (ipynb 환경에서 정의된 것):
#   gemini_client, GEMINI_MODELS, types, coverage_sector_list, wics_stock_df


# ============================================================
# 0. 유틸
# ============================================================

def _hash_id(s: str, n: int = 12) -> str:
    return hashlib.sha1(s.encode('utf-8')).hexdigest()[:n]


def _safe_isoformat(dt_val) -> str | None:
    if pd.isna(dt_val) or dt_val is None or str(dt_val) in ('nan', 'NaT', ''):
        return None
    if isinstance(dt_val, str):
        return dt_val[:19]
    try:
        return pd.Timestamp(dt_val).isoformat()
    except Exception:
        return str(dt_val)


# ============================================================
# 1. WICS slug 매핑 (Step 3-C 산출물 로드)
# ============================================================

def load_wics_slug_map(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============================================================
# 2. 종목 가격 차트 다운로드 (yfinance → fdr fallback)
# ============================================================

def download_chart(code: str, last_trading_day: str,
                   verbose: bool = True) -> dict | None:
    """fdr 우선, yf fallback. 빈 data면 1회 재시도."""
    end_date = datetime.strptime(last_trading_day, "%Y-%m-%d")
    start_date = (end_date - timedelta(days=380)).strftime("%Y-%m-%d")

    for attempt in range(2):  # 최대 2회 시도
        # fdr
        try:
            import FinanceDataReader as fdr
            df = fdr.DataReader(code, start_date)
            if not df.empty:
                df = df.reset_index()
                if datetime.now().hour < 16:
                    today_str = datetime.today().date().strftime("%Y-%m-%d")
                    df = df[df['Date'].astype(str).str[:10] != today_str]
                data = [{"date": pd.Timestamp(r['Date']).strftime("%Y-%m-%d"), "close": float(r['Close'])}
                        for _, r in df.iterrows() if pd.notna(r.get('Close'))]
                if data:
                    return {"code": code, "ticker": code,
                            "fetched_at": datetime.now().isoformat(timespec='seconds'),
                            "currency": "KRW", "data": data}
        except Exception as e:
            if verbose and attempt == 0:
                print(f"  [fdr {code}] 실패: {e}")

        # yf fallback
        try:
            import yfinance as yf
            for suffix in ('.KS', '.KQ'):
                ticker = code + suffix
                df = yf.download(ticker, start=start_date,
                                 end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                                 progress=False, auto_adjust=True)
                if not df.empty:
                    df = df.reset_index()
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    close_col = 'Close' if 'Close' in df.columns else 'Adj Close'
                    data = [{"date": pd.Timestamp(r['Date']).strftime("%Y-%m-%d"), "close": round(float(r[close_col]))}
                            for _, r in df.iterrows() if pd.notna(r[close_col])]
                    if data:
                        if verbose:
                            print(f"  [yf fallback {code}] {ticker} → {len(data)}건")
                        return {"code": code, "ticker": ticker,
                                "fetched_at": datetime.now().isoformat(timespec='seconds'),
                                "currency": "KRW", "data": data}
        except Exception as e:
            if verbose and attempt == 0:
                print(f"  [yf {code}] fallback 실패: {e}")

        if attempt == 0:
            time.sleep(2)  # 재시도 전 대기

    return None


def download_charts_batch(active_codes: list[str], last_trading_day: str,
                          out_dir: str, max_workers: int = 4,
                          verbose: bool = True) -> dict[str, bool]:
    """
    active_codes: 차트 다운로드 대상 종목코드 리스트
    Returns: {code: success?} — 카드에 has_chart 플래그용
    """
    os.makedirs(out_dir, exist_ok=True)
    result = {}
    lock = threading.Lock()
    completed = [0]
    total = len(active_codes)

    def _work(code):
        chart = download_chart(code, last_trading_day, verbose=False)
        with lock:
            completed[0] += 1
            if chart is not None:
                with open(os.path.join(out_dir, f"{code}.json"), 'w', encoding='utf-8') as f:
                    json.dump(chart, f, ensure_ascii=False, separators=(',', ':'))
                result[code] = True
                if verbose:
                    print(f"[CHART {completed[0]}/{total}] {code} | {len(chart['data'])} days")
            else:
                result[code] = False
                if verbose:
                    print(f"[CHART {completed[0]}/{total}] {code} | FAIL")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_work, active_codes))

    # 실패한 종목 재시도 (sequential, 2회)
    failed = [c for c, ok in result.items() if not ok]
    if failed:
        print(f"[CHART] {len(failed)}개 실패 → 재시도...")
        for code in failed:
            for attempt in range(2):
                time.sleep(1)
                chart = download_chart(code, last_trading_day, verbose=False)
                if chart is not None:
                    with open(os.path.join(out_dir, f"{code}.json"), 'w', encoding='utf-8') as f:
                        json.dump(chart, f, ensure_ascii=False, separators=(',', ':'))
                    result[code] = True
                    print(f"  [RETRY] {code} → OK (attempt {attempt+1})")
                    break
            else:
                print(f"  [RETRY] {code} → 최종 실패")
    return result


def get_price_change_from_chart(chart: dict) -> tuple[float | None, float | None]:
    """차트의 마지막 2거래일에서 (price, change_pct) 추출. 실패 시 (None, None)."""
    data = chart.get('data', [])
    if len(data) < 2:
        return (None, None)
    last = data[-1]['close']
    prev = data[-2]['close']
    if prev == 0:
        return (last, None)
    return (last, round((last - prev) / prev * 100, 2))


# ============================================================
# 3. 컨센서스 DataFrame → JSON 변환 (CNS_DT/YYMM 사용)
# ============================================================

def _format_yymm(yymm, term: str) -> str:
    """YYMM(202612) → '2026년' (Y) or '2026년 2분기' (Q)"""
    try:
        s = str(int(yymm))
        if len(s) != 6:
            return ""
        year = s[:4]
        month = int(s[4:])
        if term == 'Y':
            return f"{year}년"
        # Q: month 3=1Q, 6=2Q, 9=3Q, 12=4Q
        q = (month + 2) // 3
        return f"{year}년 {q}분기"
    except Exception:
        return ""


def serialize_consensus(cons_df: pd.DataFrame | None, term: str = 'Y') -> dict:
    """
    cons_df: get_cons_df 결과 (Q 또는 Y).

    Returns: {
        "period":     "2026년 2분기" or "2026년" (가장 최근 데이터의 기간),
        "latest":     1234567 (가장 최근 값, 원),
        "is_new":     bool (가장 최근 데이터가 last_trading_day에 변동했는지),
        "series":     [{"date": "2026-04-29", "value": 1234567}, ...]  # 시간순 (오래된→최신)
    }
    데이터 없으면 빈 dict {}.
    """
    if cons_df is None or cons_df.empty:
        return {}

    df = cons_df.copy()
    if 'CNS_DT' in df.columns:
        df = df.sort_values('CNS_DT', ascending=True)  # 오래된 → 최신

    series = []
    for _, row in df.iterrows():
        val = float(row['VAL']) * 1000 if pd.notna(row.get('VAL')) else None
        if val is None:
            continue
        cns_dt_raw = row.get('CNS_DT')
        date_str = None
        if pd.notna(cns_dt_raw):
            try:
                s = str(int(cns_dt_raw))
                if len(s) == 8:
                    date_str = f"{s[:4]}-{s[4:6]}-{s[6:]}"
            except Exception:
                date_str = str(cns_dt_raw)
        series.append({"date": date_str, "value": val})

    if not series:
        return {}

    # 최근 데이터 기준 메타
    last_row = df.iloc[-1]
    return {
        "period":  _format_yymm(last_row.get('YYMM'), term),
        "latest":  series[-1]['value'],
        "is_new":  bool(last_row.get('is_new', False)),
        "series":  series,
    }


# ============================================================
# 4. 종목 dict 빌드
# ============================================================

def _sentiment_kor_to_en(s) -> str:
    if s in ('positive', 'negative', 'neutral'):
        return s
    return {'긍정': 'positive', '부정': 'negative', '중립': 'neutral'}.get(str(s), 'neutral')


def build_stock_dict(code: str, name: str,
                     chart: dict | None,
                     disclosures: list[dict] | None,
                     research: list[dict] | None,
                     consensus: dict | None,
                     comments: dict | None = None,
                     stk_flow: list[dict] | None = None,
                     market_cap: float | None = None,
                     fwd_pe: float | None = None,
                     fwd_pb: float | None = None,
                     market: str | None = None,
                     briefing: dict | None = None) -> dict:
    """
    consensus = {"Q": df_q, "Y": df_y} 또는 None
    comments  = {"selected_ids": [...], "comments": {id: {...}}} 또는 None
    stk_flow  = [{"date", "외인", "개인", "기관"}, ...] 최신순 7건 또는 None
    market    = "KSP" or "KSQ"
    """
    price, change_pct = (None, None)
    if chart:
        price, change_pct = get_price_change_from_chart(chart)

    # disclosures
    discl_out = []
    for d in (disclosures or []):
        discl_out.append({
            "date":   _safe_isoformat(d.get('published_at')),
            "title":  d.get('title', ''),
            "url":    d.get('content_url') or d.get('url', ''),
            "is_new": bool(d.get('is_new', False)),
        })

    # reports
    reports_out = []
    for r in (research or []):
        ri = r.get('research_insight') or {}
        ri_summary = ri.get('summary') if isinstance(ri, dict) else None
        ri_keypoints = ri.get('key_points') if isinstance(ri, dict) else None
        if not isinstance(ri_keypoints, list):
            ri_keypoints = []
        reports_out.append({
            "broker":      r.get('publisher', ''),
            "title":       r.get('title', ''),
            "url":         r.get('content_url') or r.get('url', ''),
            "date":        _safe_isoformat(r.get('published_at')),
            "summary":     str(ri_summary or r.get('summary', ''))[:1000],
            "key_points":  [str(k) for k in ri_keypoints[:5]],
            "is_new":      bool(r.get('is_new', False)),
        })

    # consensus (라인차트 형식)
    cons_q = serialize_consensus(consensus.get('Q'), term='Q') if consensus else {}
    cons_y = serialize_consensus(consensus.get('Y'), term='Y') if consensus else {}

    # 종목별 뉴스
    news_out = []
    if comments and comments.get('selected_ids'):
        for nid in comments['selected_ids']:
            meta = comments.get('comments', {}).get(nid, {})
            news_out.append({
                "id":           nid,
                "title":        meta.get('title', ''),
                "summary":      str(meta.get('summary', ''))[:1000],
                "url":          meta.get('url', ''),
                "published_at": _safe_isoformat(meta.get('published_at')),
                "sentiment":    _sentiment_kor_to_en(meta.get('sentiment', 'neutral')),
                "is_new":       True,
            })

    # 신규 이슈 카운트 (당일 변동) — 빨간점 표시용
    new_news = sum(1 for n in news_out if n.get('is_new'))
    new_disc = sum(1 for d in discl_out if d.get('is_new'))
    new_repo = sum(1 for r in reports_out if r.get('is_new'))
    new_cons = (1 if cons_q.get('is_new') else 0) + (1 if cons_y.get('is_new') else 0)
    has_new = (new_news + new_disc + new_repo + new_cons) > 0

    # briefing 첫 문장 제거 ("주가가 X% 상승/하락하여..." 정형 문구)
    brief_out = {}
    if briefing and briefing.get('briefing'):
        raw = briefing['briefing']
        parts = raw.split('. ', 1)
        cleaned = parts[1].strip() if len(parts) > 1 and len(parts[1].strip()) > 10 else raw
        brief_out = {**briefing, 'briefing': cleaned}

    return {
        "code":       code,
        "name":       name,
        "market":     market,
        "price":      price,
        "change_pct": change_pct,
        "market_cap": market_cap,
        "fwd_pe":     fwd_pe,
        "fwd_pb":     fwd_pb,
        "has_chart":  chart is not None,
        "has_new":    has_new,
        "news":       news_out,
        "disclosures": discl_out,
        "reports":    reports_out,
        "consensus": {
            "Q": cons_q,
            "Y": cons_y,
        },
        "briefing": brief_out,
        "stk_flow": stk_flow or [],
    }


# ============================================================
# 5. AI 요약 (Gemini 3 bullet)
# ============================================================

AI_SUMMARY_PROMPT = """너는 한국 주식시장 셀사이드 애널리스트다.
주어진 한 섹터의 오늘자 이슈들(뉴스, 미국 시장 동향, 종목별 공시/컨센변동/타사리포트)을
보고, 펀드매니저가 가장 먼저 알아야 할 핵심 이슈 최대 3개를 한 줄씩 뽑아라.

규칙:
- 각 줄 15~40자, 간단명료, 사족 금지, 당연한 말 금지
- 좋은 예: "카타르에너지 LNG선 2차 발주 임박, 국내 3사 수혜"
- 나쁜 예: "조선업이 활황이다", "투자자 관심 증가"
- 정보가 빈약하면 1~2개만 반환해도 됨
- JSON 배열로만 반환"""


def _build_summary_input(sector_data: dict) -> str:
    """섹터의 모든 이슈 정보를 짧게 압축해서 Gemini 입력으로."""
    parts = []
    if sector_data.get('news'):
        parts.append("[뉴스]")
        for n in sector_data['news'][:5]:
            parts.append(f"- {n['title'][:80]}")
    if sector_data.get('us_movers'):
        parts.append("[미국 시장]")
        for m in sector_data['us_movers'][:5]:
            parts.append(f"- {m['name']} {m['change_pct']:+.1f}%: {m.get('reason','')[:50]}")
    new_disclosures = []
    new_consensus = []
    new_reports = []
    new_stock_news = []
    for s in sector_data.get('stocks', []):
        for d in s.get('disclosures', []):
            if d.get('is_new'):
                new_disclosures.append(f"{s['name']}: {d['title'][:60]}")
        # consensus는 이제 dict (Q/Y 각각 시리즈)
        cy = s.get('consensus', {}).get('Y', {})
        if isinstance(cy, dict) and cy.get('is_new') and cy.get('series'):
            series = cy['series']
            if len(series) >= 2:
                prev_v = series[-2]['value']
                cur_v = series[-1]['value']
                if prev_v and prev_v != 0:
                    pct = (cur_v - prev_v) / abs(prev_v) * 100
                    new_consensus.append(f"{s['name']} 연간컨센 {pct:+.1f}%")
        for r in s.get('reports', []):
            if r.get('is_new'):
                new_reports.append(f"{s['name']}: {r['title'][:60]}")
        for n in s.get('news', []):
            new_stock_news.append(f"{s['name']}: {n['title'][:60]}")
    if new_disclosures:
        parts.append("[신규 공시]")
        parts.extend(f"- {x}" for x in new_disclosures[:5])
    if new_consensus:
        parts.append("[신규 컨센변동]")
        parts.extend(f"- {x}" for x in new_consensus[:5])
    if new_reports:
        parts.append("[신규 타사리포트]")
        parts.extend(f"- {x}" for x in new_reports[:5])
    if new_stock_news:
        parts.append("[종목 핵심뉴스]")
        parts.extend(f"- {x}" for x in new_stock_news[:8])
    return "\n".join(parts)


def generate_ai_summaries(sectors_data: dict[str, dict],
                          gemini_client, GEMINI_MODELS: list[str], types,
                          max_workers: int = 2,
                          verbose: bool = True) -> dict[str, list[str]]:
    """
    sectors_data: {slug: sector_dict}
    Returns: {slug: ["bullet1", "bullet2", "bullet3"]}
    """
    SCHEMA = {
        "type": "ARRAY",
        "items": {"type": "STRING"},
        "maxItems": 3,
    }

    def _gemini_fb(config, contents, round_waits=(30, 60, 120)):
        """
        GEMINI_MODELS 리스트 순서대로 호출 시도. 429/RESOURCE_EXHAUSTED 시 다음 모델로.
        모든 모델이 한 라운드에서 다 실패하면 round_waits 만큼 대기 후 다시 라운드 재시도.
        """
        if not GEMINI_MODELS:
            raise ValueError("GEMINI_MODELS 리스트가 비어있음")
        last_err = None
        total_rounds = 1 + len(round_waits)
        for round_idx in range(total_rounds):
            if round_idx > 0:
                wait = round_waits[round_idx - 1]
                if verbose:
                    print(f"  [Gemini] 모든 모델 실패 → {wait}초 대기 후 라운드 {round_idx+1}/{total_rounds} 재시도")
                time.sleep(wait)
            for m in GEMINI_MODELS:
                try:
                    return gemini_client.models.generate_content(
                        model=m, contents=contents, config=config
                    )
                except Exception as e:
                    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                        last_err = e
                        continue
                    raise
        raise last_err or RuntimeError("Gemini 모든 모델/라운드 실패")

    def _call(slug: str, sector_data: dict) -> tuple[str, list[str]]:
        input_text = _build_summary_input(sector_data)
        if not input_text.strip():
            return slug, []
        config = types.GenerateContentConfig(
            system_instruction=AI_SUMMARY_PROMPT,
            response_mime_type="application/json",
            response_schema=SCHEMA,
            temperature=0.3,
        )
        try:
            resp = _gemini_fb(config, input_text)
            bullets = json.loads(resp.text)[:3]
            time.sleep(0.5)
            return slug, bullets
        except Exception as e:
            # 최종 실패 (라운드 재시도까지 다 실패) → 빈 bullets fallback
            if verbose:
                print(f"  [AI {slug}] 최종 실패: {e}")
            return slug, []

    result = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_call, slug, data): slug for slug, data in sectors_data.items()}
        for fut in as_completed(futures):
            slug, bullets = fut.result()
            result[slug] = bullets
            if verbose:
                print(f"[AI] {slug}: {len(bullets)} bullets")
    return result


# ============================================================
# 6. 섹터 JSON 빌드
# ============================================================

def _calc_weighted_return(stocks: list[dict]) -> float | None:
    """종목별 단순평균 등락률. 등락률 결측 종목은 제외."""
    if not stocks:
        return None
    vals = [float(s['change_pct']) for s in stocks if s.get('change_pct') is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def build_sector_json(wics: str, slug: str,
                      news_list: list[dict],
                      us_movers: list[dict],
                      stocks: list[dict],
                      trading_date: str,
                      previous_trading_date: str,
                      display_date: str, 
                      sector_return: float | None = None,
                      global_research: list[dict] | None = None,
                      market_data: dict | None = None) -> dict:
    parts = wics.split('-')
    return {
        "slug":                  slug,
        "wics_1st":              parts[0] if len(parts) > 0 else "",
        "wics_2nd":              parts[1] if len(parts) > 1 else "",
        "wics_3rd":              parts[2] if len(parts) > 2 else "",
        "trading_date":          trading_date,
        "previous_trading_date": previous_trading_date,
        "display_date":          display_date,
        "generated_at":          datetime.now().isoformat(timespec='seconds'),
        "sector_return":         sector_return,
        "ai_summary":            [],
        "news":                  news_list,
        "global_research":       global_research or [],
        "us_movers":             us_movers,
        "stocks":                stocks,
        "market_data":           market_data or {"charts": [], "tables": []},
    }


def build_news_for_sector(top_news_for_sector: list[dict],
                          trading_date: str) -> list[dict]:
    """top_news_for_sector: select_top_news()의 한 섹터분 결과 리스트."""
    out = []
    for art in top_news_for_sector:
        url = art.get('content_url', '')
        out.append({
            "id":           _hash_id(url) if url else _hash_id(art.get('title', '')),
            "title":        art.get('title', ''),
            "summary":      art.get('summary', ''),
            "keyword":      art.get('keyword', ''),
            "url":          url,
            "source":       art.get('publisher', ''),
            "published_at": _safe_isoformat(art.get('published_at')),
            "sentiment":    art.get('sentiment', 'neutral'),
            "source_type":  "news",
            "is_new":       _safe_isoformat(art.get('published_at', ''))[:10] >= trading_date if art.get('published_at') else False,
        })
    return out


# ============================================================
# 7. Index JSON 빌드
# ============================================================

def build_index_json(sectors_data: dict[str, dict],
                     trading_date: str,
                     display_date: str) -> dict:
    sector_list = []
    for slug, data in sectors_data.items():
        # 오늘자 신규 카운트
        new_count = 0
        for n in data.get('news', []):
            if n.get('is_new'): new_count += 1
        for s in data.get('stocks', []):
            for d in s.get('disclosures', []):
                if d.get('is_new'): new_count += 1
            for r in s.get('reports', []):
                if r.get('is_new'): new_count += 1
            cy = s.get('consensus', {}).get('Y', {})
            cq = s.get('consensus', {}).get('Q', {})
            if isinstance(cy, dict) and cy.get('is_new'): new_count += 1
            if isinstance(cq, dict) and cq.get('is_new'): new_count += 1
        # 7일 누적 (전체 카운트)
        total_count = (
            len(data.get('news', []))
            + sum(len(s.get('disclosures', [])) for s in data.get('stocks', []))
            + sum(len(s.get('reports', [])) for s in data.get('stocks', []))
        )
        sector_list.append({
            "slug":               slug,
            "wics_1st":           data['wics_1st'],
            "wics_2nd":           data['wics_2nd'],
            "wics_3rd":           data['wics_3rd'],
            "sector_return":      data.get('sector_return'),
            "issue_count_today":  new_count,
            "issue_count_total":  total_count,
            "stock_count":        len(data.get('stocks', [])),
            "us_mover_count":     len(data.get('us_movers', [])),
        })
    # 1st level → 2nd level → 3rd level 알파벳순
    sector_list.sort(key=lambda s: (s['wics_1st'], s['wics_2nd'], s['wics_3rd']))
    return {
        "generated_at": datetime.now().isoformat(timespec='seconds'),
        "trading_date": trading_date,
        "display_date": display_date, 
        "sectors":      sector_list,
    }


# ============================================================
# 8. End-to-end runner
# ============================================================

def run_serialization(
    *,
    repo_root: str,                                # GitHub 레포 로컬 클론 경로
    trading_date: str,                              # last_trading_day (YYYY-MM-DD)
    previous_trading_date: str,                     # 그 직전 영업일 (is_new 비교용)
    display_date: str,                              # 노출 일자
    coverage_sector_list: list[str],                # 45개 WICS 3rd-level
    wics_stock_df: pd.DataFrame,                    # 종목 ↔ WICS
    main_combined_stock: pd.DataFrame,              # 커버 종목 (정담당)
    top_news: dict,                                 # select_top_news 결과
    kis_gr_news: dict,                              # NEW: 글로벌 리서치 (섹터별)
    us_movers_dict: dict,                           # build_us_movers_dict 결과
    disclosure_dict: dict,
    research_dict: dict,
    consensus_dict: dict,
    comment_dict: dict,                             # fetch_stock_comments 결과
    stk_flow_7d: dict,                              # {code: [{date, 외인, 개인, 기관}]}
    fwd_pe_dict: dict,                              # NEW: {code: fwd_pe}
    fwd_pb_dict: dict,                              # NEW: {code: fwd_pb}
    code_to_market: dict,                           # {code: 'KSP'/'KSQ'}
    briefing_dict: dict,                            # {code: {briefing, content_url, ...}}
    market_data_by_slug: dict,                      # {slug: {charts: [], tables: []}}
    wics_slug_map: dict,                            # WICS → slug
    gemini_client, GEMINI_MODELS: list[str], types,
    chart_max_workers: int = 4,
    summary_max_workers: int = 2,
    verbose: bool = True,
) -> None:
    """
    모든 데이터를 받아 GitHub 레포 디렉토리에 JSON 파일들을 생성.
    """
    data_dir = os.path.join(repo_root, 'data')
    sectors_dir = os.path.join(data_dir, 'sectors')
    charts_dir = os.path.join(data_dir, 'charts')
    os.makedirs(sectors_dir, exist_ok=True)
    os.makedirs(charts_dir, exist_ok=True)

    # ─── 1. 활성 종목 식별 (b 정책: disclosure/research/consensus/news 보유 종목) ───
    active_codes = set()
    active_codes.update(disclosure_dict.keys())
    active_codes.update(research_dict.keys())
    active_codes.update(consensus_dict.keys())
    if comment_dict:
        active_codes.update(comment_dict.keys())
    if verbose:
        print(f"[Step4] 활성 종목 (차트 대상): {len(active_codes)}개")

    # ─── 2. 종목 가격 차트 다운로드 ───
    chart_results = download_charts_batch(
        list(active_codes), trading_date, charts_dir,
        max_workers=chart_max_workers, verbose=verbose,
    )
    chart_cache = {}
    for code in active_codes:
        if chart_results.get(code):
            with open(os.path.join(charts_dir, f"{code}.json"), 'r', encoding='utf-8') as f:
                chart_cache[code] = json.load(f)

    # ─── 3. 종목 → WICS 매핑 dict ───
    code_to_wics = wics_stock_df.assign(
        코드=wics_stock_df['종목코드'].str.lstrip('A')
    ).set_index('코드')['WICS분류'].to_dict()
    code_to_name = wics_stock_df.assign(
        코드=wics_stock_df['종목코드'].str.lstrip('A')
    ).set_index('코드')['종목명'].to_dict()
    # 시가총액 매핑 (combined_stock에 있으면)
    code_to_cap: dict[str, float] = {}
    if '시가총액' in main_combined_stock.columns:
        for _, r in main_combined_stock.iterrows():
            cap = r.get('시가총액')
            if pd.notna(cap):
                code_to_cap[str(r['코드'])] = float(cap)

    # ─── 4. 섹터별 종목 그룹핑 ───
    coverage_codes = set(main_combined_stock['코드'].astype(str))
    wics_to_codes: dict[str, list[str]] = {}
    for code in coverage_codes:
        w = code_to_wics.get(code)
        if w in coverage_sector_list:
            wics_to_codes.setdefault(w, []).append(code)

    # ─── 5. 섹터 dict 빌드 ───
    sectors_data: dict[str, dict] = {}
    for wics in coverage_sector_list:
        slug = wics_slug_map.get(wics)
        if not slug:
            if verbose: print(f"  [WARN] slug 없음: {wics}")
            continue

        # 종목 dict 리스트
        stocks_out = []
        for code in wics_to_codes.get(wics, []):
            stocks_out.append(build_stock_dict(
                code, code_to_name.get(code, code),
                chart_cache.get(code),
                disclosure_dict.get(code),
                research_dict.get(code),
                consensus_dict.get(code),
                comment_dict.get(code) if comment_dict else None,
                stk_flow_7d.get(code) if stk_flow_7d else None,
                code_to_cap.get(code),
                fwd_pe_dict.get(code) if fwd_pe_dict else None,
                fwd_pb_dict.get(code) if fwd_pb_dict else None,
                code_to_market.get(code) if code_to_market else None,
                briefing_dict.get(code) if briefing_dict else None,
            ))
        # 가격 변동 큰 순 정렬
        stocks_out.sort(key=lambda s: abs(s.get('change_pct') or 0), reverse=True)

        # 섹터 등락률 (시총가중평균)
        sector_return = _calc_weighted_return(stocks_out)

        # news
        news_out = build_news_for_sector(top_news.get(wics, []), trading_date)

        # us_movers
        us_out = us_movers_dict.get(wics, [])

        # global research (KIS 글로벌 리서치)
        gr_out = kis_gr_news.get(wics, []) if kis_gr_news else []

        sectors_data[slug] = build_sector_json(
            wics, slug, news_out, us_out, stocks_out,
            trading_date, previous_trading_date,
            display_date=display_date,
            sector_return=sector_return,
            global_research=gr_out,
            market_data=market_data_by_slug.get(slug) if market_data_by_slug else None,
        )

    # ─── 6. AI 요약 생성 ───
    if verbose: print(f"[Step4] AI 요약 생성: {len(sectors_data)}개 섹터")
    summaries = generate_ai_summaries(
        sectors_data, gemini_client, GEMINI_MODELS, types,
        max_workers=summary_max_workers, verbose=verbose,
    )
    for slug, bullets in summaries.items():
        if slug in sectors_data:
            sectors_data[slug]['ai_summary'] = bullets

    # ─── 7. 섹터 JSON 저장 (날짜 suffix) ───
    for slug, data in sectors_data.items():
        out_path = os.path.join(sectors_dir, f"{slug}_{trading_date}.json")
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    if verbose: print(f"[Step4] sectors/*_{trading_date}.json 저장 완료 ({len(sectors_data)}개)")

    # ─── 8. index.json 저장 (날짜 suffix) ───
    index_data = build_index_json(sectors_data, trading_date, display_date)
    index_path = os.path.join(data_dir, f'index_{trading_date}.json')
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, ensure_ascii=False, separators=(',', ':'))
    if verbose: print(f"[Step4] index_{trading_date}.json 저장 완료")

    # ─── 9. wics_slug_map.json 저장 (참조용, 날짜 무관 overwrite) ───
    with open(os.path.join(data_dir, 'wics_slug_map.json'), 'w', encoding='utf-8') as f:
        json.dump(wics_slug_map, f, ensure_ascii=False, indent=2)

    # ─── 10. 최근 5영업일 외 파일 prune + dates.json 갱신 ───
    prune_old_data_files(data_dir, sectors_dir, keep_n_business_days=5, verbose=verbose)

    if verbose:
        print(f"[Step4] 직렬화 완료. 활성 섹터 {len(sectors_data)}개, "
              f"활성 종목 {len(active_codes)}개, 차트 성공 {sum(chart_results.values())}개")


# ============================================================
# 9. 파일 prune + dates.json 갱신
# ============================================================

def prune_old_data_files(data_dir: str, sectors_dir: str,
                          keep_n_business_days: int = 5,
                          verbose: bool = True) -> None:
    """
    1) data/ 와 sectors/ 의 모든 날짜-suffix 파일을 스캔
    2) 가용 날짜 목록 추출
    3) 최근 N영업일(KRX 캘린더 기준) 외의 파일 삭제
    4) data/dates.json 생성/갱신
    
    파일명 규칙:
    - data/index_YYYY-MM-DD.json
    - data/sectors/{slug}_YYYY-MM-DD.json
    """
    import re
    import glob as _glob
    
    DATE_RE = re.compile(r'_(\d{4}-\d{2}-\d{2})\.json$')
    
    # ─── 1. 가용 날짜 추출 (index_*.json 기준) ───
    index_files = _glob.glob(os.path.join(data_dir, 'index_*.json'))
    available_dates = set()
    for f in index_files:
        m = DATE_RE.search(os.path.basename(f))
        if m:
            available_dates.add(m.group(1))
    
    if not available_dates:
        if verbose: print("[Prune] 가용 날짜 없음, 스킵")
        return
    
    # ─── 2. KRX 캘린더 기준 최근 N영업일 산정 ───
    # 가장 최근 날짜를 기준으로 거꾸로 N영업일
    sorted_dates = sorted(available_dates, reverse=True)
    latest = sorted_dates[0]
    
    try:
        import pandas_market_calendars as mcal
        krx = mcal.get_calendar('XKRX')
        # 충분히 넓은 윈도우(30일)로 잡고, latest 이전 N영업일 cutoff 계산
        end = pd.Timestamp(latest)
        start = end - pd.Timedelta(days=30)
        schedule = krx.schedule(start_date=start, end_date=end)
        business_days = [d.strftime('%Y-%m-%d') for d in schedule.index.date]
        # latest 포함 직전 N영업일
        keep_set = set(business_days[-keep_n_business_days:]) if business_days else {latest}
    except Exception as e:
        if verbose: print(f"[Prune] mcal 실패 ({e}) → 단순 최근 N개 사용")
        keep_set = set(sorted_dates[:keep_n_business_days])
    
    # 추가로 latest는 무조건 보존 (mcal 결과에 latest가 안 들어가는 엣지케이스 방어)
    keep_set.add(latest)
    
    # ─── 3. index_*.json 삭제 ───
    deleted = 0
    for f in index_files:
        m = DATE_RE.search(os.path.basename(f))
        if m and m.group(1) not in keep_set:
            try:
                os.remove(f)
                deleted += 1
            except OSError as e:
                if verbose: print(f"[Prune] {f} 삭제 실패: {e}")
    
    # ─── 4. sectors/{slug}_YYYY-MM-DD.json 삭제 ───
    sector_files = _glob.glob(os.path.join(sectors_dir, '*_*.json'))
    for f in sector_files:
        m = DATE_RE.search(os.path.basename(f))
        if m and m.group(1) not in keep_set:
            try:
                os.remove(f)
                deleted += 1
            except OSError as e:
                if verbose: print(f"[Prune] {f} 삭제 실패: {e}")
    
    # ─── 5. dates.json 재생성 (살아남은 index_*.json 기준) ───
    remaining_index = _glob.glob(os.path.join(data_dir, 'index_*.json'))
    remaining_dates = []
    for f in remaining_index:
        m = DATE_RE.search(os.path.basename(f))
        if m:
            remaining_dates.append(m.group(1))
    remaining_dates.sort(reverse=True)
    
    dates_meta = {
        "available_dates": remaining_dates,           # 최신 → 과거 순
        "latest":          remaining_dates[0] if remaining_dates else None,
        "updated_at":      datetime.now().isoformat(timespec='seconds'),
    }
    with open(os.path.join(data_dir, 'dates.json'), 'w', encoding='utf-8') as f:
        json.dump(dates_meta, f, ensure_ascii=False, indent=2)
    
    if verbose:
        print(f"[Prune] keep={sorted(keep_set)} | 삭제={deleted}개 | "
              f"잔존={len(remaining_dates)}개 → dates.json 갱신")
