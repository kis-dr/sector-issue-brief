"""
US Movers 처리 로직
- xlsx 로드 → 변동 종목 필터 → 뉴스 매칭/수집 → Gemini 사유분석 + WICS 매핑
- 기존 ipynb의 _fetch_paginated, classify_wics 패턴을 따름
- ipynb에서 import해서 사용하거나, 셀에 그대로 붙여넣기 가능
"""

import glob, os, re, time, json, random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests

# ─── 외부 의존 (ipynb 환경에서 이미 정의된 것들) ────────────────────────────
# from __main__ import (
#     gemini_client, MODEL_ID, types,
#     header_deepsearch, DS_AUTH,
#     coverage_sector_list,    # 45개 커버 WICS 3rd-level
#     _fetch_paginated,        # ipynb cell 7
# )

# =============================================================================
# 1) xlsx 로드
# =============================================================================

US_FOLDER = r"\\197.197.26.121\계량분석\송준영\2026\디지털RA\US_데일리_특징_섹터\DATA"

def load_us_movers_xlsx(target_date: str, folder: str = US_FOLDER) -> pd.DataFrame | None:
    """
    target_date(YYYY-MM-DD)의 US_stk_top_1000({date}).xlsx 로드.
    파일이 없으면 None 반환 (휴장일 = US 섹션 스킵).
    """
    pattern = os.path.join(folder, f"US_stk_top_1000({target_date}).xlsx")
    files = glob.glob(pattern)
    if not files:
        print(f"[US movers] 파일 없음: {pattern} → 스킵")
        return None
    df = pd.read_excel(files[0])
    print(f"[US movers] 로드: {os.path.basename(files[0])} ({len(df):,}건)")
    return df


# =============================================================================
# 2) 변동 종목 필터
# =============================================================================

def filter_us_movers(df: pd.DataFrame, change_threshold: float = 5.0) -> pd.DataFrame:
    """|change|≥5% OR 52주 최고/최저 발생 종목만 추출."""
    cond_change = df['change'].abs() >= change_threshold
    cond_high   = df['오늘52주최고가여부'].astype(str).str.upper() == 'Y'
    cond_low    = df['오늘52주최저가여부'].astype(str).str.upper() == 'Y'
    movers = df[cond_change | cond_high | cond_low].copy()

    movers['is_52w_high']  = cond_high.loc[movers.index]
    movers['is_52w_low']   = cond_low.loc[movers.index]
    print(f"[US movers] 필터링: {len(df):,}건 → {len(movers):,}건 "
          f"(변동≥{change_threshold}%: {cond_change.sum()}, 52w_high: {cond_high.sum()}, 52w_low: {cond_low.sum()})")
    return movers.reset_index(drop=True)


# =============================================================================
# 3) 기존 뉴스 데이터에서 매칭
# =============================================================================

def _normalize_name(s: str) -> str:
    """매칭용 정규화: 소문자 + 회사 접미사 제거."""
    if not isinstance(s, str):
        return ""
    s = s.lower()
    for suffix in [' inc', ' corp', ' co', ' ltd', ' llc', ' plc',
                   ' holdings', ' group', ' company', ' corporation', '.', ',']:
        s = s.replace(suffix, '')
    return re.sub(r'\s+', ' ', s).strip()


def match_existing_news(movers: pd.DataFrame, *news_dfs: pd.DataFrame,
                         max_per_stock: int = 10) -> dict[str, list[dict]]:
    """
    movers의 각 종목에 대해 기존 뉴스 DataFrame들에서 ticker/이름 매칭.
    Returns: {ticker: [{title, summary, published_at, source}, ...]}
    """
    pool = []
    for df in news_dfs:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            pool.append({
                'title':        str(row.get('title', '')),
                'summary':      str(row.get('summary', ''))[:300],
                'published_at': str(row.get('published_at', '')),
                'source':       str(row.get('source', row.get('publisher', ''))),
                'symbols':      row.get('symbols', []) if isinstance(row.get('symbols', []), list) else [],
                'norm_text':    _normalize_name(str(row.get('title', '')) + ' ' + str(row.get('summary', '')))
            })

    matched = {}
    for _, mv in movers.iterrows():
        ticker = mv['Symbol']
        name_en = _normalize_name(str(mv['name']))
        name_ko = str(mv.get('한글종목명', ''))
        hits = []
        for art in pool:
            # ticker symbol 매칭 (DeepSearch는 보통 NYSE:AAPL, NASDAQ:AAPL 형태)
            if any(ticker in s for s in art['symbols']):
                hits.append(art); continue
            # 영문/한글 이름 매칭 (>=4자 이상일 때만 정밀도 OK)
            if len(name_en) >= 4 and name_en in art['norm_text']:
                hits.append(art); continue
            if len(name_ko) >= 2 and name_ko in art['title']:
                hits.append(art); continue

        # 최신순 정렬
        hits.sort(key=lambda x: x['published_at'], reverse=True)
        matched[ticker] = hits[:max_per_stock]
    return matched


# =============================================================================
# 4) 부족분 DeepSearch 호출 (어제자, 종목당 최대 10건)
# =============================================================================

def fetch_movers_news_deepsearch(tickers: list[str], date_str: str,
                                  exchange_map: dict[str, str],
                                  max_per_stock: int = 10,
                                  batch_size: int = 20,
                                  header_deepsearch: str = None,
                                  ds_auth=None) -> dict[str, list[dict]]:
    """
    DeepSearch global-articles?symbols=... 호출.
    여러 ticker를 batch_size 단위로 한 번에 호출.
    Returns: {ticker: [...]}
    """
    if not tickers:
        return {}

    # symbols 파라미터: NYSE:AAPL,NASDAQ:MSFT,...
    def to_symbol(t):
        ex = exchange_map.get(t, '')
        return f"{ex}:{t}" if ex else t

    by_ticker = {t: [] for t in tickers}
    batches = [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]

    for batch in batches:
        symbols = ",".join(to_symbol(t) for t in batch)
        params = (f"symbols={symbols}&date_from={date_str}&date_to={date_str}"
                  f"&page_size=100")
        try:
            data = _fetch_paginated(  # noqa: F821 (ipynb 외부 정의)
                "https://api-v2.deepsearch.com/v1/global-articles",
                params, max_pages=5,
            )
        except Exception as e:
            print(f"  [DeepSearch] batch 실패: {e}")
            continue

        # ticker별로 다시 분배 (한 기사가 여러 symbols 포함 가능)
        for art in data:
            art_symbols = art.get('symbols', []) or []
            title = art.get('title_ko') or art.get('title') or ''
            summary = art.get('summary_ko') or art.get('summary') or ''
            entry = {
                'title':        title,
                'summary':      str(summary)[:300],
                'published_at': str(art.get('published_at', '')),
                'source':       art.get('publisher', ''),
                'symbols':      art_symbols,
            }
            for sym in art_symbols:
                # symbols는 "NYSE:AAPL" 또는 "AAPL" 형태
                t = sym.split(':')[-1]
                if t in by_ticker:
                    by_ticker[t].append(entry)

    # 최신순 정렬 + 종목당 max
    for t in by_ticker:
        by_ticker[t].sort(key=lambda x: x['published_at'], reverse=True)
        by_ticker[t] = by_ticker[t][:max_per_stock]
    return by_ticker


# =============================================================================
# 5) Gemini: 사유 분석 + WICS 멀티 매핑 (한 번의 호출)
# =============================================================================

ANALYSIS_PROMPT = """너는 한국 주식시장 셀사이드 애널리스트다.

미국 종목 리스트가 주어진다. 각 종목마다:
- 일간 변동률(change_pct)과 52주 최고/최저 발생 여부
- 어제자 관련 뉴스 (최대 5건, 최신순)

이를 보고 두 가지를 산출하라:

1. reason: 변동의 핵심 원인을 한 줄로 요약 (15~40자)
   - 사족 금지, 당연한 말 금지, 결정적 사유만
   - 좋은 예: "1Q26 실적 컨센 상회 + 데이터센터 가이던스 상향"
   - 나쁜 예: "주가가 상승했다", "투자자 관심 증가"
   - 뉴스가 부실해서 사유 추정 불가하면 "사유 미상"

2. wics_sectors: 이 종목의 사업이 영향을 미치는 **한국 시장 WICS 3rd-level** (멀티 가능)
   - 제공된 enum 옵션 중에서만 선택
   - 한 종목이 여러 섹터에 영향: 모두 선택 (예: NVDA → 반도체 + AI 활용 산업)
   - 한국 시장 연결고리가 약하면 빈 배열 []
   - 보수적으로: 직접적 사업/공급망 연결만 인정

JSON 배열로 반환. 입력 종목 순서와 동일하게."""


def analyze_movers_with_gemini(movers: pd.DataFrame,
                                news_by_ticker: dict[str, list[dict]],
                                coverage_sectors: list[str],
                                gemini_client, MODEL_ID: str, types,
                                batch_size: int = 10,
                                max_news_per_stock: int = 5) -> pd.DataFrame:
    """
    movers DataFrame에 reason, wics_sectors 컬럼 추가.
    Gemini 한 번 호출로 두 작업을 동시에 처리.
    """
    if movers.empty:
        return movers.assign(reason=None, wics_sectors=[[] for _ in range(len(movers))])

    SCHEMA = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "reason": {"type": "STRING"},
                "wics_sectors": {
                    "type": "ARRAY",
                    "items": {"type": "STRING", "enum": coverage_sectors}
                }
            },
            "required": ["reason", "wics_sectors"]
        }
    }

    # 입력 데이터 구성
    inputs = []
    for _, mv in movers.iterrows():
        news_list = news_by_ticker.get(mv['Symbol'], [])[:max_news_per_stock]
        flag = []
        if mv.get('is_52w_high'): flag.append("52주최고")
        if mv.get('is_52w_low'):  flag.append("52주최저")
        inputs.append({
            "ticker":     mv['Symbol'],
            "name":       str(mv['name']),
            "change_pct": round(float(mv['change']), 2),
            "flag":       flag,
            "news":       [{"t": n['title'], "s": n['summary'][:200]} for n in news_list],
        })

    batches = [inputs[i:i+batch_size] for i in range(0, len(inputs), batch_size)]
    results: list[dict] = [None] * len(inputs)

    def _call(batch_idx: int) -> tuple[int, list[dict]]:
        batch_inputs = batches[batch_idx]
        for attempt in range(5):
            try:
                resp = gemini_client.models.generate_content(
                    model=MODEL_ID,
                    contents=json.dumps(batch_inputs, ensure_ascii=False),
                    config=types.GenerateContentConfig(
                        system_instruction=ANALYSIS_PROMPT,
                        response_mime_type="application/json",
                        response_schema=SCHEMA,
                        temperature=0.0,
                    ),
                )
                parsed = json.loads(resp.text)
                # 결과 길이 검증
                if len(parsed) != len(batch_inputs):
                    print(f"  [Gemini] batch {batch_idx} 길이 불일치: {len(parsed)} vs {len(batch_inputs)}")
                    parsed = parsed + [{"reason": "사유 미상", "wics_sectors": []}] * (len(batch_inputs) - len(parsed))
                time.sleep(0.5)
                return batch_idx, parsed
            except Exception as e:
                if "429" in str(e):
                    wait = 2 ** (attempt + 1) + random.uniform(0, 2)
                    print(f"  [Gemini] batch {batch_idx} rate limited, {wait:.1f}s 대기")
                    time.sleep(wait)
                else:
                    if attempt >= 2:
                        print(f"  [Gemini] batch {batch_idx} 실패: {e}")
                        return batch_idx, [{"reason": "사유 미상", "wics_sectors": []}] * len(batch_inputs)
                    time.sleep(2 ** (attempt + 1))
        return batch_idx, [{"reason": "사유 미상", "wics_sectors": []}] * len(batch_inputs)

    # rate limit 고려해 max_workers=2
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = [ex.submit(_call, i) for i in range(len(batches))]
        for fut in as_completed(futures):
            bi, parsed = fut.result()
            start = bi * batch_size
            for j, item in enumerate(parsed):
                if start + j < len(inputs):
                    results[start + j] = item

    movers = movers.copy()
    movers['reason']       = [r['reason'] for r in results]
    movers['wics_sectors'] = [r['wics_sectors'] for r in results]
    return movers


# =============================================================================
# 6) WICS별 dict로 산출 (멀티 매핑 → 같은 mover가 여러 섹터에 들어감)
# =============================================================================

def aggregate_movers_by_wics(movers: pd.DataFrame) -> dict[str, list[dict]]:
    """
    {wics_sector: [mover_dict, ...]} 형태로 산출.
    한 mover가 여러 wics_sectors에 매핑되어 있으면 양쪽에 모두 포함.
    """
    out: dict[str, list[dict]] = {}
    for _, mv in movers.iterrows():
        sectors = mv.get('wics_sectors') or []
        if not sectors:
            continue
        entry = {
            "ticker":      mv['Symbol'],
            "exchange":    mv.get('거래소', ''),
            "name":        mv['name'],
            "name_ko":     mv.get('한글종목명', ''),
            "price":       round(float(mv['price']), 2) if pd.notna(mv.get('price')) else None,
            "currency":    "USD",
            "change_pct":  round(float(mv['change']), 2),
            "is_52w_high": bool(mv.get('is_52w_high', False)),
            "is_52w_low":  bool(mv.get('is_52w_low', False)),
            "reason":      mv.get('reason') or "사유 미상",
        }
        for sec in sectors:
            out.setdefault(sec, []).append(entry)

    # 섹터별로 |change_pct| 큰 순 정렬
    for sec in out:
        out[sec].sort(key=lambda x: abs(x['change_pct']), reverse=True)
    return out


# =============================================================================
# 7) End-to-end runner (ipynb 셀에서 호출)
# =============================================================================

def build_us_movers_dict(last_trading_day: str,
                          kr_df: pd.DataFrame,
                          us_df: pd.DataFrame,
                          kis_gr_df: pd.DataFrame,
                          coverage_sectors: list[str],
                          gemini_client, MODEL_ID: str, types,
                          header_deepsearch: str, ds_auth) -> dict[str, list[dict]]:
    """
    US movers 처리 end-to-end.
    Returns: {wics_3rd: [movers...]} — sectors/{slug}.json에 그대로 사용 가능
    """
    # 1) 로드
    raw = load_us_movers_xlsx(last_trading_day)
    if raw is None:
        return {}

    # 2) 필터
    movers = filter_us_movers(raw)
    if movers.empty:
        return {}

    # 3) 기존 데이터에서 매칭
    matched = match_existing_news(movers, kr_df, us_df, kis_gr_df, max_per_stock=10)
    have_news = {t for t, lst in matched.items() if lst}
    missing = [t for t in movers['Symbol'] if t not in have_news]
    print(f"[US movers] 매칭: {len(have_news)}/{len(movers)} 종목, 추가 fetch 필요: {len(missing)}개")

    # 4) 부족분 fetch
    if missing:
        exchange_map = dict(zip(movers['Symbol'], movers['거래소']))
        ds_news = fetch_movers_news_deepsearch(
            missing, last_trading_day, exchange_map,
            max_per_stock=10,
            header_deepsearch=header_deepsearch, ds_auth=ds_auth,
        )
        for t, news in ds_news.items():
            matched[t] = news

    # 5) Gemini 분석 (사유 + WICS 멀티)
    movers = analyze_movers_with_gemini(
        movers, matched, coverage_sectors,
        gemini_client, MODEL_ID, types,
        batch_size=10, max_news_per_stock=5,
    )

    # 6) WICS별 dict
    result = aggregate_movers_by_wics(movers)
    print(f"[US movers] 최종: {sum(len(v) for v in result.values())}개 매핑 "
          f"({len(result)}개 섹터에 분산)")
    return result
