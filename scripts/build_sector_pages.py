"""
빌드 스크립트: sectors/_template.html을 복사해서 sectors/{slug}.html을 45개 생성

사용법:
    python build_sector_pages.py

동작:
- data/wics_slug_map.json 을 읽어서 모든 slug 추출
- sectors/_template.html을 각 slug 이름으로 복사
- 기존 파일 있으면 덮어쓰기

실행 시점:
- WICS slug 매핑이 변경될 때만 실행 (보통 1회)
- 매일 데이터 업데이트와 무관
"""

import json
import os
import shutil
import sys
from pathlib import Path


def main(repo_root: str = None):
    if repo_root is None:
        # 스크립트 기준 상위 폴더
        repo_root = str(Path(__file__).resolve().parent.parent)

    repo = Path(repo_root)
    template = repo / "sectors" / "_template.html"
    slug_map_path = repo / "data" / "wics_slug_map.json"

    if not template.exists():
        print(f"❌ 템플릿 없음: {template}")
        sys.exit(1)
    if not slug_map_path.exists():
        print(f"❌ slug map 없음: {slug_map_path}")
        sys.exit(1)

    with open(slug_map_path, 'r', encoding='utf-8') as f:
        slug_map = json.load(f)
    slugs = set(slug_map.values())
    print(f"slug 개수: {len(slugs)}")

    sectors_dir = repo / "sectors"
    created, skipped = 0, 0
    for slug in sorted(slugs):
        target = sectors_dir / f"{slug}.html"
        shutil.copyfile(template, target)
        created += 1

    print(f"✓ 생성 완료: {created}개")
    print(f"  위치: {sectors_dir}")


if __name__ == "__main__":
    main()
