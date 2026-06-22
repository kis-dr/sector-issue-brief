"""
종목별 뉴스 수집 모듈
- fetch_comment_data 옛 코드를 단순화 (텔레그램 제거)
- 어제자 1일치, 종목당 최대 3건 핵심 뉴스 선정
- 중복 제거 (TF-IDF 코사인 유사도 0.2)
- Gemini로 핵심 뉴스 선정 + sentiment 태깅 (response_schema 사용)
- 모든 종목 공통: 증권사 의견 인용("○○증권 보고서/연구원/목표가/투자의견")
  + 단순 시황 기사("코스피 마감" 등) 제거
- 증권사 종목(WICS=금융-증권-증권): 위 필터 + 전용 시스템 프롬프트 추가 사용
"""

import json, re, time, random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import logging
logger = logging.getLogger(__name__)


# ============================================================
# 0. 증권사 종목 전용 추가 필터 (main.py의 섹터 필터에서 룰 부분만 차용)
# ============================================================
# 증권사 애널리스트 의견/리포트 인용 패턴 — 본문에 등장하면 "증권사가 분석 주체"
_SECURITIES_OPINION_PATTERNS = re.compile(
    r"(증권|證|투자증권|투자\s*증권)\s*(보고서|리포트|연구원|애널리스트)"
    r"|목표\s*(주?가|치|지수)\s*(상향|하향|유지|제시|올|내)"
    r"|투자의견\s*(매수|중립|매도|비중확대|비중축소|유지|상향|하향)"
    r"|투자\s*의견\s*(매수|중립|매도|비중확대|비중축소|유지|상향|하향)"
    r"|(BUY|Buy|SELL|Sell|HOLD|Hold)\s*의견"
    r"|증권가\s*(전망|분석|예상|관측|시각|시선)",
)

# 단순 시황 키워드 (제목 기반 사전 차단)
_MARKET_TICKER_TITLE_PATTERNS = re.compile(
    r"오늘의\s*증시|마감\s*시황|개장\s*시황|증시\s*전망|증시\s*마감"
    r"|코스피\s*(강세|약세|상승|하락|급등|급락|등락|마감|출발|돌파)"
    r"|코스닥\s*(강세|약세|상승|하락|급등|급락|등락|마감|출발|돌파)"
    r"|뉴욕증시\s*(상승|하락|마감|급등|급락)"
    r"|나스닥\s*(상승|하락|마감|급등|급락)"
    r"|다우\s*(상승|하락|마감|최고치|급등|급락)",
)

# 증권사 실적/거래대금 영향 분석 — 시황 기사여도 이런 키워드 있으면 유지
_SECURITIES_PROFIT_IMPACT_PATTERNS = re.compile(
    r"증권사\s*(실적|순이익|영업이익|수수료|수익)"
    r"|거래대금\s*(폭증|증가|급증).{0,30}(증권|수수료|수익)"
    r"|(증권|투자증권).{0,20}(분기|연간)\s*실적"
    r"|브로커리지\s*(수익|수수료)",
)


def _is_securities_opinion_article(title: str, summary: str) -> bool:
    """'증권사 의견/리포트 인용' 형태 판정 (룰 기반)."""
    return bool(_SECURITIES_OPINION_PATTERNS.search(f"{title} {summary}"))


def _is_pure_market_ticker(title: str, summary: str) -> bool:
    """단순 시황 기사 판정 (단, 증권사 실적 영향 분석은 제외)."""
    if _SECURITIES_PROFIT_IMPACT_PATTERNS.search(f"{title} {summary}"):
        return False
    return bool(_MARKET_TICKER_TITLE_PATTERNS.search(title))


def filter_securities_stock_news(news_df: pd.DataFrame) -> pd.DataFrame:
    """
    증권사 종목 뉴스용 추가 룰 필터.
    제거 대상:
      1) 증권사 의견 인용 기사 ("○○증권 보고서/연구원", "목표가 상향" 등)
      2) 단순 시황 기사 ("코스피 마감" 등) — 단 증권사 실적 영향 분석은 유지
    """
    if news_df.empty:
        return news_df

    def _keep(row):
        title = str(row.get('title', '') or '')
        summary = str(row.get('summary', '') or '')
        if _is_securities_opinion_article(title, summary):
            return False
        if _is_pure_market_ticker(title, summary):
            return False
        return True

    mask = news_df.apply(_keep, axis=1)
    return news_df[mask].reset_index(drop=True)


def apply_sector_article_filter(news_df: pd.DataFrame, article_filter) -> pd.DataFrame:
    """
    종목 뉴스 DF에 main.py의 filter_articles(섹터용 룰 필터)를 적용.

    종목 뉴스 DF에는 filter_articles가 요구하는 sections/named_entities 컬럼이
    없으므로 어댑터에서 채운다:
      - sections=""        → _get_sections_str()가 ""를 반환 → world 필터 스킵
      - named_entities="[]" → _has_market_entity()가 False 반환 (b-2 정책)
                              → 스포츠/연예/사회/지역정치/교육 등 카테고리 노이즈
                                필터가 활성화됨
    이벤트/프로모션(promo_event), 사진캡션, 인사, 지수노이즈, 애널리스트리포트
    등도 함께 제거된다. filter_articles는 (passed, removed) 튜플을 반환하므로
    passed만 사용한다.
    """
    if article_filter is None or news_df.empty:
        return news_df

    df = news_df.copy()
    # filter_articles가 참조하는 누락 컬럼 보강 (없을 때만)
    if 'sections' not in df.columns:
        df['sections'] = ""
    if 'named_entities' not in df.columns:
        df['named_entities'] = "[]"

    passed, _removed = article_filter(df)
    # filter_articles는 보조 컬럼을 제거하고 원본 컬럼만 남겨 반환하므로
    # 후속 파이프라인(remove_similar_articles 등)에 그대로 넘길 수 있다.
    return passed.reset_index(drop=True)


# ============================================================
# 1. 중복 제거 (옛 remove_similar_articles 그대로)
# ============================================================
def remove_similar_articles(df: pd.DataFrame,
                             title_threshold: float = 0.2,
                             summary_threshold: float = 0.2) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy().reset_index(drop=True)

    def get_sim_matrix(texts):
        vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4), min_df=1)
        return cosine_similarity(vec.fit_transform(texts))

    title_sim   = get_sim_matrix(df["title"].fillna("").tolist())
    summary_sim = get_sim_matrix(df["summary"].fillna("").tolist())
    sim_matrix = (title_sim >= title_threshold) | (summary_sim >= summary_threshold)
    sorted_idx = df["published_at"].argsort()[::-1].tolist()
    to_drop = set()
    for i in sorted_idx:
        if i in to_drop:
            continue
        for j in sorted_idx:
            if i == j or j in to_drop:
                continue
            if sim_matrix[i, j]:
                to_drop.add(j)
    return df.drop(index=list(to_drop)).reset_index(drop=True)


# ============================================================
# 2. Gemini fallback 헬퍼
# ============================================================

def _gemini_call_with_fallback(gemini_client, config, contents, models,
                                round_waits=(30, 60, 120)):
    """
    GEMINI_MODELS 리스트 순서대로 호출 시도. 429/RESOURCE_EXHAUSTED 시 다음 모델로.
    모든 모델이 한 라운드에서 다 실패하면 round_waits 만큼 대기 후 다시 라운드 재시도.
    최종 실패 시 마지막 에러 raise.
    """
    if not models:
        raise ValueError("GEMINI_MODELS 리스트가 비어있음")

    last_err = None
    # 라운드 0 = 즉시 시도, 라운드 1~ = 대기 후 재시도
    total_rounds = 1 + len(round_waits)
    for round_idx in range(total_rounds):
        if round_idx > 0:
            wait = round_waits[round_idx - 1]
            logger.warning(f"  [Gemini] 모든 모델 실패 → {wait}초 대기 후 라운드 {round_idx+1}/{total_rounds} 재시도")
            time.sleep(wait)
        for m in models:
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


# ============================================================
# 3. Gemini 선정 + sentiment 태깅 (response_schema 사용)
# ============================================================

NEWS_SELECTION_PROMPT = """너는 한국 증권사 시니어 에쿼티 애널리스트다.
주어진 종목의 뉴스 목록에서 **비중요 뉴스를 제거**하고 남은 것 중 **최대 3개**를 선정하라.

제거 기준 (아래에 해당하면 탈락):
- 스포츠/연예/사회/날씨/단순 인사 등 펀더멘털 무관
- 동일 사건 중복 (가장 정보량 많은 것만 남김)
- 단순 시황 요약 / 지수 리캡 (예: "코스피 상승 마감")
- 광고성/프로모션/이벤트 홍보

나머지는 모두 유의미한 뉴스로 간주. 특히 아래는 반드시 포함:
1. 실적: 매출/영업이익/순이익 서프라이즈, 가이던스 변경
2. 수급: 대규모 지분 변동, 자사주, 블록딜, 공매도
3. 기업 이벤트: M&A, 분할, 증자, CB/BW
4. 규제/정책: 업종 직접 영향 법안/관세/보조금
5. 산업 수급: 원자재 가격, 공급망, 수주/계약
6. 매크로: 금리, 환율, 신용등급
7. 경영 리스크: CEO 교체, 소송/과징금
8. 밸류에이션 리레이팅, 목표주가 변경, 투자의견 변경
9. 신규 사업/제품/기술 관련 뉴스

각 뉴스마다 sentiment를 "positive"/"negative"/"neutral" 중 하나로 분류.
**최신 뉴스일수록 높은 가중치 부여.** 동일 중요도면 발행 시각이 더 최근인 뉴스 우선.
빈약하면 빈 배열도 OK."""


# 증권사 종목(WICS=금융-증권-증권) 전용 프롬프트 — 위의 일반 프롬프트를 강화
SECURITIES_NEWS_SELECTION_PROMPT = """너는 한국 증권사 시니어 에쿼티 애널리스트다.
**이 종목은 증권사(브로커리지 회사)다.** 종목 자체의 펀더멘털 뉴스만 선정하라.
주어진 뉴스 목록에서 **비중요 뉴스를 제거**하고 남은 것 중 **최대 3개**를 선정하라.

[증권사 종목 — 매우 흔한 오분류, 반드시 제외]
- 이 증권사의 애널리스트가 **다른 종목/산업**을 분석한 기사 ("○○증권 보고서", "○○증권 연구원", "목표가 상향/하향", "투자의견 매수/중립")
- 코스피·코스닥·뉴욕증시·나스닥 단순 시황 (이 증권사 실적 영향 분석이 없는 경우)
- 다른 기사에서 이 증권사 코멘트가 부수적으로 인용된 경우
- "증권가 전망/분석/예상/관측" 등 증권업계 종합 코멘트성 시황

[증권사 종목 — 선정해야 할 뉴스]
1. 이 증권사의 실적: 매출/영업이익/순이익/수수료수익/IB수익 발표, 가이던스
2. 거래대금 폭증이 이 증권사 수익에 미치는 영향 (실적 직결 분석)
3. M&A, 지배구조 변경, 대주주/지분 변동, CEO 교체
4. 신사업 진출 (가상자산, IB, WM, 디지털 등)
5. 금융당국 제재·소송·검찰 수사 (이 증권사 또는 임직원 대상)
6. 증권업 전체에 영향 주는 규제 (공매도, 신용공여, 사모펀드 규제 등)
7. 펀드 판매 채널로서 수익에 영향 주는 정책

각 뉴스마다 sentiment를 "positive"/"negative"/"neutral" 중 하나로 분류.
**최신 뉴스일수록 높은 가중치 부여.** 판단이 애매하면 보수적으로 제외하라.
선정할 만한 뉴스가 없으면 빈 배열도 OK."""


def select_news_with_sentiment(df: pd.DataFrame, stock_code: str,
                                gemini_client, GEMINI_MODELS: list[str], types,
                                max_input: int = 30, max_retry: int = 3,
                                is_securities: bool = False) -> dict:
    """
    Returns: {
        "selected_ids": [...],
        "comments": {id: {"title", "summary", "url", "sentiment"}}
    }
    is_securities=True 이면 증권사 종목 전용 시스템 프롬프트 사용.
    """
    if df.empty:
        return {"selected_ids": [], "comments": {}}

    df = df.head(max_input).copy()

    SCHEMA = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "id":        {"type": "STRING"},
                "sentiment": {"type": "STRING", "enum": ["positive", "negative", "neutral"]},
            },
            "required": ["id", "sentiment"],
        },
        "maxItems": 3,
    }

    inputs = [
        {"id": str(r["id"]),
         "t":  str(r["title"]),
         "s":  str(r.get("summary", ""))[:200]}
        for _, r in df.iterrows()
    ]

    user_content = (f"종목: {stock_code}\n오늘: {datetime.now().strftime('%Y-%m-%d')}\n\n"
                    + json.dumps(inputs, ensure_ascii=False))

    system_prompt = SECURITIES_NEWS_SELECTION_PROMPT if is_securities else NEWS_SELECTION_PROMPT
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=SCHEMA,
        temperature=0.1,
    )
    parsed = None
    try:
        resp = _gemini_call_with_fallback(
            gemini_client, config, user_content, GEMINI_MODELS
        )
        parsed = json.loads(resp.text)[:3]
    except Exception as e:
        # 최종 실패 (라운드 재시도까지 다 실패) → 최신 3건 fallback
        logger.error(f"  [comment_data] Gemini 최종 실패 ({e}) → 최신 3건 fallback")
        top3 = df.sort_values("published_at", ascending=False).head(3)
        fb_ids = top3["id"].astype(str).tolist()
        fb_comments = {}
        for _, row in top3.iterrows():
            nid = str(row["id"])
            fb_comments[nid] = {
                "title": row.get("title", ""),
                "summary": str(row.get("summary", ""))[:1000],
                "url": row.get("content_url", ""),
                "published_at": str(row.get("published_at", "")),
                "sentiment": "neutral",
            }
        return {"selected_ids": fb_ids, "comments": fb_comments}

    valid_ids  = set(df["id"].astype(str))
    id_to_meta = df.set_index(df["id"].astype(str))[
        ["title", "summary", "content_url", "published_at"]
    ].to_dict("index")

    selected_ids = []
    comments = {}
    for item in parsed:
        nid = str(item.get("id", ""))
        if nid in valid_ids and nid not in selected_ids:
            selected_ids.append(nid)
            meta = id_to_meta.get(nid, {})
            comments[nid] = {
                "title":        meta.get("title", ""),
                "summary":      str(meta.get("summary", ""))[:1000],
                "url":          meta.get("content_url", ""),
                "published_at": str(meta.get("published_at", "")),
                "sentiment":    item.get("sentiment", "neutral"),
            }
    return {"selected_ids": selected_ids, "comments": comments}


# ============================================================
# 3. End-to-end: 종목별 뉴스 dict 빌드
# ============================================================

def fetch_stock_comments(all_codes: list[str], last_trading_day: str,
                         header_deepsearch: str, ds_auth,
                         gemini_client, GEMINI_MODELS: list[str], types,
                         securities_codes: set[str] | None = None,
                         page_size: int = 100, max_workers: int = 4,
                         filter_empty: bool = True, verbose: bool = True,
                         article_filter=None) -> dict:
    """
    Returns: {code: {selected_ids: [...], comments: {id: {...}}}}

    securities_codes: WICS=금융-증권-증권 종목 코드 set.
      이 set에 포함된 종목은 (1) 룰 기반 추가 필터 적용
                            (2) Gemini 호출 시 증권사 전용 시스템 프롬프트 사용

    article_filter: main.py의 filter_articles 함수 (선택). 주입되면 모든 종목
      뉴스에 섹터용 룰 필터(이벤트/프로모션/스포츠/연예 등)를 추가 적용한다.
      종목 뉴스 DF에는 sections/named_entities 컬럼이 없으므로 어댑터에서
      sections=""(world 필터 스킵), named_entities="[]"(has_ent=False, 카테고리
      노이즈 필터 활성화) 로 채워 호출한다. 순환 import를 피하려고 함수를 직접
      주입받는다.
    """
    BASE_URL = ("https://api-v2.deepsearch.com/v1/articles/"
                "politics,economy,society,culture,world,tech,opinion")
    COLUMNS = ["id", "title", "publisher", "summary", "published_at", "content_url"]

    if securities_codes is None:
        securities_codes = set()

    comment_dict = {}
    import threading
    lock = threading.Lock()
    completed = [0]
    total = len(all_codes)

    def _work(code):
        try:
            url = (f"{BASE_URL}?symbols=KRX:{code}"
                   f"&date_from={last_trading_day}"
                   f"&clustering=false&uniquify=true&research_insight=true"
                   f"&page_size={page_size}")
            resp = requests.get(url, headers={"Authorization": header_deepsearch},
                                auth=ds_auth, verify=False)
            resp.raise_for_status()
            data = resp.json().get("data", [])

            is_sec = code in securities_codes

            with lock:
                completed[0] += 1
                progress = f"[CMT {completed[0]}/{total}] {code}{' (증권)' if is_sec else ''}"

            if not data:
                if verbose: logger.info(f"{progress} | 0건")
                return

            news_df = (pd.DataFrame(data)[COLUMNS]
                       .sort_values("published_at", ascending=False)
                       .reset_index(drop=True))

            # 기본 전처리 필터 (Gemini 전)
            EXCLUDE_PUB = {'이데일리','조선일보','중앙일보','동아일보',
                           '뉴데일리','폴리뉴스','데일리안','시민의소리'}
            NOISE_PAT = re.compile(
                r'부고|부음|고향|고졸|수능|입시|결혼|이혼|맛집|여행|날씨|'
                r'축구|야구|농구|골프|올림픽|스포츠|연예|가요|드라마|영화|'
                r'로또|복권|인사발령|승진|정기인사|취임식|임명장'
            )
            def _basic_filter(row):
                if not row.get('title','').strip():
                    return False
                if row.get('publisher','') in EXCLUDE_PUB:
                    return False
                t = row.get('title','') + ' ' + row.get('summary','')
                if NOISE_PAT.search(t):
                    return False
                return True
            mask = news_df.apply(_basic_filter, axis=1)
            news_df = news_df[mask].reset_index(drop=True)

            # ── 증권사 의견 인용 + 단순 시황 필터 (모든 종목 공통) ──
            #   filter_securities_stock_news 가 이름은 '증권' 같지만
            #   1) "○○증권 보고서/연구원/목표가/투자의견" 인용 기사
            #   2) "코스피·코스닥·뉴욕증시·나스닥 단순 시황" 기사
            #   를 모두 제거. 이는 일반 종목 코멘트에서도 노이즈이므로 공통 적용.
            n_before_broker_filter = len(news_df)
            news_df = filter_securities_stock_news(news_df)
            n_after_broker_filter = len(news_df)

            # ── 섹터용 룰 필터 (이벤트/프로모션/스포츠/연예 등) — 주입 시에만 ──
            #   main.py의 filter_articles를 그대로 재사용 (b-2 정책).
            n_after_sector_filter = n_after_broker_filter
            if article_filter is not None:
                news_df = apply_sector_article_filter(news_df, article_filter)
                n_after_sector_filter = len(news_df)

            news_df_unique = remove_similar_articles(news_df)
            result = select_news_with_sentiment(
                news_df_unique, code, gemini_client, GEMINI_MODELS, types,
                is_securities=is_sec,
            )

            with lock:
                if result.get("selected_ids"):
                    comment_dict[code] = result
                if verbose:
                    broker_msg = (f" → 증권/시황필터 {n_after_broker_filter}"
                                  if n_before_broker_filter != n_after_broker_filter
                                  else "")
                    sector_msg = (f" → 섹터필터 {n_after_sector_filter}"
                                  if (article_filter is not None
                                      and n_after_sector_filter != n_after_broker_filter)
                                  else "")
                    logger.info(f"{progress} | "
                          f"raw {n_before_broker_filter}{broker_msg}{sector_msg} → "
                          f"dedup {len(news_df_unique)} → "
                          f"selected {len(result.get('selected_ids', []))}")
        except Exception as e:
            with lock:
                completed[0] += 1
                if verbose:
                    logger.warning(f"[CMT {completed[0]}/{total}] {code} ERR: {e}")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_work, all_codes))

    if filter_empty:
        comment_dict = {k: v for k, v in comment_dict.items() if v.get("selected_ids")}
    return comment_dict
