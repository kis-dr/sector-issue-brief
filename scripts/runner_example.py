# =============================================================================
# Step 4-6 통합 셀 — ipynb 마지막에 추가
# =============================================================================
import sys, os, re, glob, subprocess
from datetime import datetime, timedelta
sys.path.insert(0, './scripts')

from us_movers import build_us_movers_dict
from comment_data import fetch_stock_comments
from serialize import load_wics_slug_map, run_serialization
from email_sender import send_sector_emails  # Step 6

REPO_ROOT = r"C:\path\to\sector-issue-brief"   # ← 송이의 로컬 git clone 경로

# 1) WICS slug 매핑 로드
wics_slug_map = load_wics_slug_map(os.path.join(REPO_ROOT, 'data', 'wics_slug_map.json'))

# 2) 직전 영업일 산정 (stock_check 파일 기준)
stock_check_files = sorted(glob.glob(os.path.join(folder_path, "stock_check*.xlsx")))
m = re.search(r'\d{4}-\d{2}-\d{2}', stock_check_files[-2])
previous_trading_date = m.group()

# 3) US movers 처리
us_movers_dict = build_us_movers_dict(
    last_trading_day=last_trading_day,
    kr_df=kr_df, us_df=us_df, kis_gr_df=kis_gr_df,
    coverage_sectors=coverage_sector_list,
    gemini_client=gemini_client, MODEL_ID=MODEL_ID, types=types,
    header_deepsearch=header_deepsearch, ds_auth=DS_AUTH,
)

# 4) 종목별 뉴스 처리
all_codes = main_combined_stock['코드'].astype(str).tolist()
comment_dict = fetch_stock_comments(
    all_codes=all_codes, last_trading_day=last_trading_day,
    header_deepsearch=header_deepsearch, ds_auth=DS_AUTH,
    gemini_client=gemini_client, MODEL_ID=MODEL_ID, types=types,
)

# 5) JSON 직렬화 + AI 요약
run_serialization(
    repo_root=REPO_ROOT,
    trading_date=last_trading_day,
    previous_trading_date=previous_trading_date,
    coverage_sector_list=coverage_sector_list,
    wics_stock_df=wics_stock_df,
    main_combined_stock=main_combined_stock,
    top_news=top_news,
    us_movers_dict=us_movers_dict,
    disclosure_dict=disclosure_dict,
    research_dict=research_dict,
    consensus_dict=consensus_dict,
    comment_dict=comment_dict,
    wics_slug_map=wics_slug_map,
    gemini_client=gemini_client, MODEL_ID=MODEL_ID, types=types,
)

# 6) git push (GitHub Pages 자동 재배포)
subprocess.run(['git', '-C', REPO_ROOT, 'add', 'data/'], check=True)
subprocess.run(['git', '-C', REPO_ROOT, 'commit', '-m', f'data: {last_trading_day}'], check=True)
subprocess.run(['git', '-C', REPO_ROOT, 'push'], check=True)

# 7) 섹터 애널리스트 이메일 발송
send_sector_emails(
    repo_root=REPO_ROOT,
    trading_date=last_trading_day,
    analyst_df=analyst_df,
    main_combined_stock=main_combined_stock,    # 종목↔담당자↔WICS분류
    coverage_sector_list=coverage_sector_list,
    wics_slug_map=wics_slug_map,
    pages_base_url="https://[유저명].github.io/sector-issue-brief",  # ← 변경
    smtp_user=os.getenv("GMAIL_USER"),
    smtp_pass=os.getenv("GMAIL_APP_PASS"),
    wics_col="WICS분류",                         # ← combined_stock 컬럼명 확인 필요
    test_recipient="112796@koreainvestment.com",
)
print(f"✓ {last_trading_day} 완료")
