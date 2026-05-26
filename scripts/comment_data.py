"""
종목별 뉴스 수집 모듈
- fetch_comment_data 옛 코드를 단순화 (텔레그램 제거)
- 어제자 1일치, 종목당 최대 3건 핵심 뉴스 선정
- 중복 제거 (TF-IDF 코사인 유사도 0.2)
- Gemini로 핵심 뉴스 선정 + sentiment 태깅 (response_schema 사용)
"""

import json, re, time, random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


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
            print(f"  [Gemini] 모든 모델 실패 → {wait}초 대기 후 라운드 {round_idx+1}/{total_rounds} 재시도")
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


def select_news_with_sentiment(df: pd.DataFrame, stock_code: str,
                                gemini_client, GEMINI_MODELS: list[str], types,
                                max_input: int = 30, max_retry: int = 3) -> dict:
    """
    Returns: {
        "selected_ids": [...],
        "comments": {id: {"title", "summary", "url", "sentiment"}}
    }
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

    config = types.GenerateContentConfig(
        system_instruction=NEWS_SELECTION_PROMPT,
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
        print(f"  [comment_data] Gemini 최종 실패 ({e}) → 최신 3건 fallback")
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
                         page_size: int = 100, max_workers: int = 4,
                         filter_empty: bool = True, verbose: bool = True) -> dict:
    """
    Returns: {code: {selected_ids: [...], comments: {id: {...}}}}
    """
    BASE_URL = ("https://api-v2.deepsearch.com/v1/articles/"
                "politics,economy,society,culture,world,tech,opinion")
    COLUMNS = ["id", "title", "publisher", "summary", "published_at", "content_url"]

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

            with lock:
                completed[0] += 1
                progress = f"[CMT {completed[0]}/{total}] {code}"

            if not data:
                if verbose: print(f"{progress} | 0건")
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

            news_df_unique = remove_similar_articles(news_df)
            result = select_news_with_sentiment(
                news_df_unique, code, gemini_client, GEMINI_MODELS, types
            )

            with lock:
                if result.get("selected_ids"):
                    comment_dict[code] = result
                if verbose:
                    print(f"{progress} | "
                          f"raw {len(news_df)} → dedup {len(news_df_unique)} → "
                          f"selected {len(result.get('selected_ids', []))}")
        except Exception as e:
            with lock:
                completed[0] += 1
                if verbose:
                    print(f"[CMT {completed[0]}/{total}] {code} ERR: {e}")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(_work, all_codes))

    if filter_empty:
        comment_dict = {k: v for k, v in comment_dict.items() if v.get("selected_ids")}
    return comment_dict
