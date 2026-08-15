#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
令和8年熊本地震 支援情報自動更新スクリプト
-------------------------------------------------
1. 国・熊本県・宇城市（小川町含む）・J-Net21 等の公式サイト/RSSから最新情報を収集
2. Gemini API で「支援情報かどうかの判定」と「構造化データへの変換・要約」を実施
3. public/data/support_info.json を更新（重複は source_url をキーにマージ）

環境変数:
    GEMINI_API_KEY  ... Google AI Studio の Gemini API キー（必須）

実行方法:
    python scripts/update_data.py

GitHub Actions からは daily-update.yml 経由で毎朝実行される想定。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

# --------------------------------------------------------------------------
# 依存ライブラリ（requirements.txt を参照）
#   requests, beautifulsoup4, feedparser, python-dateutil
# --------------------------------------------------------------------------
import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dateutil import tz

# --------------------------------------------------------------------------
# 基本設定
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT_DIR / "public" / "data" / "support_info.json"
JST = tz.gettz("Asia/Tokyo")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# 収集後、この日数より古い記事はスキップ（初回のノイズを減らすため）。
# 0 にすると日付フィルタなし。
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "0"))

# support_info.json 内に保持しておく最大件数（古いものから切り捨て）
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "300"))

REQUEST_TIMEOUT = 20
# HTTPヘッダーはASCII(latin-1)のみ許容されるため、日本語を含めないこと。
# 連絡先や用途はリポジトリURLのみで示す。
USER_AGENT = (
    "KumamotoEarthquakeSupportBot/1.0 "
    "(+https://github.com/yyymkto/kumamoto-support-portal)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("update_data")


# --------------------------------------------------------------------------
# 収集対象ソース定義
#   type: "rss" or "html"
#   html の場合は list_selector で記事リンクのCSSセレクタを指定する
# --------------------------------------------------------------------------
@dataclass
class Source:
    name: str
    url: str
    type: str  # "rss" | "html"
    default_region: list[str] = field(default_factory=list)
    list_selector: Optional[str] = None  # html only: 記事<a>タグのCSSセレクタ
    base_url: Optional[str] = None  # html only: 相対リンク解決用


SOURCES: list[Source] = [
    # 宇城市 公式RSS（新着情報）
    Source(
        name="宇城市 新着情報RSS",
        url="https://www.city.uki.kumamoto.jp/rss/news/",
        type="rss",
        default_region=["宇城市", "小川町"],
    ),
    # 宇城市 地震特設ページ（トップの緊急情報。RSS対象外の更新も拾うためHTML併用）
    Source(
        name="宇城市 地震関連情報ページ",
        url="https://www.city.uki.kumamoto.jp/toppage/kinkyu/2606699",
        type="html",
        default_region=["宇城市", "小川町"],
        list_selector="a",  # サイト構造の変化に強くするため、あえて広め(a)に設定
        base_url="https://www.city.uki.kumamoto.jp",
    ),
    # 熊本県 地震関連情報ページ
    Source(
        name="熊本県 令和8年熊本地震に関する情報",
        url="https://www.pref.kumamoto.jp/soshiki/1/274517.html",
        type="html",
        default_region=["熊本県全域", "宇城市"],
        list_selector="a",
        base_url="https://www.pref.kumamoto.jp",
    ),
    # J-Net21 熊本地震 特設支援情報ページ（中小企業・事業者向け）
    Source(
        name="J-Net21 令和8年熊本地震に関する支援情報",
        url="https://j-net21.smrj.go.jp/solution/bcp-disaster-response/kumamoto.html",
        type="html",
        default_region=["熊本県全域", "宇城市"],
        list_selector="a",
        base_url="https://j-net21.smrj.go.jp",
    ),
    # 氷川町（宇城市の隣接自治体。同じく震度7を観測し、生活圏・商圏が重なる）
    Source(
        name="氷川町 令和8年熊本地震関連情報",
        url="https://www.town.hikawa.kumamoto.jp/list00849.html",
        type="html",
        default_region=["氷川町"],
        list_selector="a",
        base_url="https://www.town.hikawa.kumamoto.jp",
    ),
    # 熊本市（周辺自治体の中で最大の商圏。事業者向け情報が充実している）
    Source(
        name="熊本市 令和8年熊本地震関連情報",
        url="https://www.city.kumamoto.jp/list04828.html",
        type="html",
        default_region=["熊本市", "熊本県全域"],
        list_selector="a",
        base_url="https://www.city.kumamoto.jp",
    ),
]

# 支援情報として拾いたいキーワード（タイトル/本文フィルタ用）
# ここに引っかからない一般ニュース（お祭り情報など）は Gemini に送る前に除外し、
# API 呼び出しコストとノイズを削減する。
# ※ 当初は絞りすぎて候補が少なくなっていたため、関連しそうな語をやや広めに追加している。
RELEVANT_KEYWORDS = [
    "地震", "支援", "補助", "助成", "融資", "貸付", "相談窓口", "義援金",
    "見舞金", "罹災", "り災", "被災", "仮設", "応急", "住宅", "中小企業",
    "小規模事業者", "商店", "事業者", "生活再建", "生活再建支援", "税",
    "減免", "猶予", "ボランティア", "宇城", "小川", "氷川", "熊本地震",
    "熊本市", "八代", "災害", "見舞", "給付", "貸出", "延長", "共済",
    "事業再開", "復旧", "商工会議所", "商工会", "経営相談", "特別貸付",
    "セーフティネット", "入浴", "給水", "断水", "停電", "避難所",
]


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    title: str
    link: str
    snippet: str
    published_date: Optional[str]  # ISO date string (YYYY-MM-DD) が分かれば
    source_name: str
    default_region: list[str]


def make_id(url: str) -> str:
    """source_url からユニークかつ安定したIDを生成する。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def http_get(url: str) -> Optional[requests.Response]:
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp
    except requests.RequestException as e:
        log.warning("取得失敗: %s (%s)", url, e)
        return None


# --------------------------------------------------------------------------
# 収集: RSS
# --------------------------------------------------------------------------
def fetch_rss(source: Source) -> Optional[list[Candidate]]:
    log.info("RSS取得中: %s", source.url)
    resp = http_get(source.url)
    if resp is None:
        return None  # 取得失敗（0件とは区別する）

    feed = feedparser.parse(resp.content)
    candidates: list[Candidate] = []
    for entry in feed.entries:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        snippet = getattr(entry, "summary", "") or getattr(entry, "description", "")
        snippet = BeautifulSoup(snippet, "html.parser").get_text(" ", strip=True)

        published_date = None
        if getattr(entry, "published_parsed", None):
            published_date = dt.date(*entry.published_parsed[:3]).isoformat()
        elif getattr(entry, "updated_parsed", None):
            published_date = dt.date(*entry.updated_parsed[:3]).isoformat()

        if not title or not link:
            continue

        candidates.append(
            Candidate(
                title=title,
                link=link,
                snippet=snippet,
                published_date=published_date,
                source_name=source.name,
                default_region=source.default_region,
            )
        )
    log.info("  -> %d 件取得", len(candidates))
    return candidates


# --------------------------------------------------------------------------
# 収集: HTML（一覧ページから記事リンクを抽出）
# --------------------------------------------------------------------------
DATE_PATTERN = re.compile(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})")


def guess_date_from_text(text: str) -> Optional[str]:
    m = DATE_PATTERN.search(text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def fetch_html(source: Source) -> Optional[list[Candidate]]:
    log.info("HTML取得中: %s", source.url)
    resp = http_get(source.url)
    if resp is None:
        return None  # 取得失敗（0件とは区別する）

    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.select(source.list_selector or "a")

    candidates: list[Candidate] = []
    seen_links: set[str] = set()
    for a in anchors:
        title = a.get_text(" ", strip=True)
        href = a.get("href")
        if not title or not href:
            continue
        if len(title) < 6:  # 「詳細はこちら」等のノイズを除外
            continue

        link = requests.compat.urljoin(source.base_url or source.url, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        # 周辺テキスト（親要素）から日付を推定
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        published_date = guess_date_from_text(parent_text) or guess_date_from_text(title)

        candidates.append(
            Candidate(
                title=title,
                link=link,
                snippet=parent_text[:300],
                published_date=published_date,
                source_name=source.name,
                default_region=source.default_region,
            )
        )
    log.info("  -> %d 件取得（フィルタ前）", len(candidates))
    return candidates


def is_relevant(candidate: Candidate) -> bool:
    text = f"{candidate.title} {candidate.snippet}"
    return any(kw in text for kw in RELEVANT_KEYWORDS)


def within_age_limit(candidate: Candidate) -> bool:
    if MAX_AGE_DAYS <= 0 or not candidate.published_date:
        return True
    try:
        d = dt.date.fromisoformat(candidate.published_date)
    except ValueError:
        return True
    return (dt.date.today() - d).days <= MAX_AGE_DAYS


def collect_all_candidates() -> tuple[list[Candidate], int, int]:
    """収集結果に加え、(成功したソース数, 全ソース数) を返す。

    「その日は新着の支援情報が0件だった」は異常ではないが、
    「全ソースへのアクセス自体が失敗した」場合はサイト構造変更や
    ネットワーク障害の可能性が高いため、呼び出し側で異常検知に使う。
    """
    all_candidates: list[Candidate] = []
    sources_ok = 0

    for source in SOURCES:
        try:
            if source.type == "rss":
                items = fetch_rss(source)
            else:
                items = fetch_html(source)
        except Exception:  # noqa: BLE001 - 1ソースの失敗で全体を止めない
            log.exception("ソース処理中にエラー: %s", source.name)
            items = None

        if items is not None:
            sources_ok += 1
        else:
            items = []

        filtered = [c for c in items if is_relevant(c) and within_age_limit(c)]
        log.info("  -> 関連情報として %d 件を採用 (%s)", len(filtered), source.name)
        all_candidates.extend(filtered)

        time.sleep(1)  # サイトへの負荷軽減

    # link (source_url) で重複排除
    dedup: dict[str, Candidate] = {}
    for c in all_candidates:
        dedup.setdefault(c.link, c)
    return list(dedup.values()), sources_ok, len(SOURCES)


# --------------------------------------------------------------------------
# Gemini API による構造化・要約
# --------------------------------------------------------------------------
EXTRACTION_SYSTEM_PROMPT = """\
あなたは、令和8年熊本地震で被災した住民・中小企業・商店経営者を支援するための
情報ポータルサイトの編集アシスタントです。

以下に渡す「タイトル」「抜粋」「リンク」の情報から、この記事が
1) 中小企業・商店・事業者向けの支援制度（融資、補助金、相談窓口、税の減免など）
2) 被災住民の生活再建支援（住宅、義援金、罹災証明、仮設住宅、生活資金、ボランティア等）
のいずれかに該当する「具体的な支援情報」であるかどうかを判定してください。

該当しない場合（一般的なお知らせ、イベント告知、地震と無関係な記事など）は
"is_support_info": false を返し、他のフィールドは null または空配列にしてください。

該当する場合は、日本語で簡潔に要約し、以下のJSON形式で**JSONのみ**を出力してください。
説明文やMarkdownのコードブロック記号は一切付けないでください。

{
  "is_support_info": true,
  "category": ["中小企業・事業者" と "生活再建" のうち該当するものを1つ以上],
  "sub_category": ["資金繰り・融資" "補助金・助成金" "相談窓口" "税・保険料" "住まい" "り災証明" "生活資金" "ボランティア・生活支援" 等から該当するものを1つ以上],
  "region": ["宇城市" "小川町" "熊本県全域" "全国" 等、対象地域が分かれば],
  "organization": "制度の実施主体（分かる範囲で）",
  "summary": "2〜3文程度の日本語要約。金額・期限・対象者などの具体情報があれば含める。",
  "target": "対象者（個人／中小企業／商店 など、分かる範囲で1文）",
  "deadline": "申請期限が明記されていればYYYY-MM-DD形式。不明ならnull"
}
"""


def call_gemini(candidate: Candidate) -> Optional[dict[str, Any]]:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY が未設定のため、構造化をスキップします。")
        return None

    user_content = (
        f"タイトル: {candidate.title}\n"
        f"抜粋: {candidate.snippet}\n"
        f"リンク: {candidate.link}\n"
        f"情報源: {candidate.source_name}\n"
        f"取得できた日付: {candidate.published_date or '不明'}\n"
    )

    payload = {
        "system_instruction": {"parts": [{"text": EXTRACTION_SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {
            "temperature": 0.2,
            "response_mime_type": "application/json",
        },
    }

    url = f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}"
    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("Gemini API 呼び出し失敗: %s (%s)", candidate.title, e)
        return None

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        log.warning("Gemini応答の形式が想定外です: %s", data)
        return None

    text = text.strip()
    # 万一コードブロックが付いてきた場合の保険
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Gemini応答のJSONパースに失敗: %s", text[:200])
        return None

    return parsed


def structure_candidate(candidate: Candidate) -> Optional[dict[str, Any]]:
    result = call_gemini(candidate)
    if not result or not result.get("is_support_info"):
        return None

    now = dt.datetime.now(tz=JST).isoformat()
    region = result.get("region") or candidate.default_region

    return {
        "id": make_id(candidate.link),
        "title": candidate.title,
        "category": result.get("category") or [],
        "sub_category": result.get("sub_category") or [],
        "region": region,
        "organization": result.get("organization") or candidate.source_name,
        "summary": result.get("summary") or candidate.snippet[:200],
        "target": result.get("target"),
        "deadline": result.get("deadline"),
        "source_url": candidate.link,
        "source_name": candidate.source_name,
        "published_date": candidate.published_date,
        "collected_at": now,
        "status": "active",
    }


# --------------------------------------------------------------------------
# 既存JSONとのマージ・保存
# --------------------------------------------------------------------------
def load_existing() -> dict[str, Any]:
    if DATA_PATH.exists():
        with DATA_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": None, "items": []}


def merge_items(existing_items: list[dict], new_items: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {item["source_url"]: item for item in existing_items}
    for item in new_items:
        by_url[item["source_url"]] = item  # 新しい情報で上書き（要約が更新される）

    merged = list(by_url.values())
    # 新しい順に並べ、上限件数で切り捨て
    merged.sort(key=lambda x: x.get("published_date") or "", reverse=True)
    return merged[:MAX_ITEMS]


def save_data(items: list[dict]) -> None:
    now = dt.datetime.now(tz=JST).isoformat()
    payload = {
        "last_updated": now,
        "generated_by": "scripts/update_data.py",
        "items": items,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DATA_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(DATA_PATH)
    log.info("保存完了: %s (%d件)", DATA_PATH, len(items))


# --------------------------------------------------------------------------
# メイン処理
# --------------------------------------------------------------------------
def main() -> int:
    log.info("=== 支援情報の収集を開始します ===")

    if not GEMINI_API_KEY:
        log.error(
            "GEMINI_API_KEY が設定されていません。"
            "リポジトリの Secrets に GEMINI_API_KEY を登録してください。"
        )
        return 1

    candidates, sources_ok, sources_total = collect_all_candidates()
    log.info(
        "収集された候補（重複排除後）: %d 件 / アクセス成功ソース: %d/%d",
        len(candidates), sources_ok, sources_total,
    )

    if sources_ok == 0:
        log.error(
            "すべての情報源へのアクセスに失敗しました。"
            "サイト構造の変更やネットワーク障害の可能性があります。"
        )
        return 1

    if not candidates:
        log.info("本日は新規の支援情報候補がありませんでした（異常ではありません）。終了します。")
        return 0

    new_items: list[dict] = []
    for i, candidate in enumerate(candidates, start=1):
        log.info("[%d/%d] Gemini構造化中: %s", i, len(candidates), candidate.title)
        structured = structure_candidate(candidate)
        if structured:
            new_items.append(structured)
        time.sleep(0.5)  # API レートリミット対策

    log.info("支援情報として採用: %d 件", len(new_items))

    existing = load_existing()
    merged = merge_items(existing.get("items", []), new_items)
    save_data(merged)

    log.info("=== 完了 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
