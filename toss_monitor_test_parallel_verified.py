import json
import os
import re
import time
import unicodedata
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import sync_playwright

# ============================================================
# TOSS監視設定
# ============================================================
BASE_URL = "https://www.cm1.eprs.jp"
URL = f"{BASE_URL}/TOSS/web/view/user/c005RsvEmptyState.html"
HOME_URL = f"{BASE_URL}/TOSS/web/view/user/c005HomeIndex.html"
EMPTY_STATE_PATH = "/TOSS/web/view/user/c005RsvEmptyState.html"

BASE_DIR = Path(__file__).resolve().parent
RESULT_FILE = BASE_DIR / "toss_status.txt"
TRACE_FILE = BASE_DIR / "toss_trace.txt"
STATE_FILE = BASE_DIR / "toss_state.json"
STATE_BACKUP_FILE = BASE_DIR / "toss_state.json.bak"
RUNTIME_FILE = BASE_DIR / "toss_runtime.json"
RUNTIME_BACKUP_FILE = BASE_DIR / "toss_runtime.json.bak"

JST = ZoneInfo("Asia/Tokyo")

# 今日を含む14日間。
DAYS_AHEAD = 13
CLOSED_WEEKDAYS = {0}  # 月曜除外

# HTTP方式では同一セッションの状態を壊さないため直列取得。
# サーバーへ短時間に連続送信しないための間隔。
REQUEST_GAP_SECONDS = 0.20
HTTP_TIMEOUT_MS = 20_000
CHECK_INTERVAL_SECONDS = 5 * 60

# システム側の夜間停止時間（JST）。TOSS取得・通知処理を行わない。
MAINTENANCE_START_HOUR = 1
MAINTENANCE_START_MINUTE = 0
MAINTENANCE_END_HOUR = 7
MAINTENANCE_END_MINUTE = 0

INITIAL_SCAN_NOTIFY = False
HEADLESS = True
HTTP_ENCODING_TRACE_LOGGED = False

TIME_MAP = {
    "10": "9時",
    "20": "13時",
    "30": "17時",
}

PURPOSES = [
    "バスケットボール",
    "バスケ（ハーフコート）",
]

STATUS_MAP = {
    "空き": "○",
    "予約": "×",
    "保守": "保守",
    "休館": "休館",
    "受付期間外": "受付期間外",
    "時間帯なし": "時間帯なし",
    "カート追加選択中": "選択中",
    "カート追加済": "選択済",
    "カート追加不可": "予約不可",
}

# この監視でHTTP POSTしてよい操作は、通常の検索結果表示に使われる2種類だけ。
ALLOWED_ACTION_FIELDS = {
    "layoutChildBody:childForm:doChangeDate",
    "layoutChildBody:childForm:doPager",
}


def now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


class MaintenanceWindowError(RuntimeError):
    """TOSSの日次メンテナンス時間に入ったため監視を中断したことを示す。"""


class ScheduledMaintenanceError(RuntimeError):
    """お知らせで当日が終日メンテナンス対象と確認できたことを示す。"""


class ScheduledSystemMaintenanceError(RuntimeError):
    """お知らせで現在がシステムメンテナンス時間帯と確認できたことを示す。"""

    def __init__(self, message, end_time):
        super().__init__(message)
        self.end_time = end_time


def is_maintenance_time(dt=None):
    current = dt or datetime.now(JST)
    start_seconds = MAINTENANCE_START_HOUR * 3600 + MAINTENANCE_START_MINUTE * 60
    end_seconds = MAINTENANCE_END_HOUR * 3600 + MAINTENANCE_END_MINUTE * 60
    current_seconds = current.hour * 3600 + current.minute * 60 + current.second
    return start_seconds <= current_seconds < end_seconds


def ensure_not_maintenance():
    if is_maintenance_time():
        raise MaintenanceWindowError(
            "夜間停止時間（01:00～07:00）のため監視を中断します。"
        )


def seconds_until_maintenance_end():
    current = datetime.now(JST)
    end = current.replace(
        hour=MAINTENANCE_END_HOUR,
        minute=MAINTENANCE_END_MINUTE,
        second=0,
        microsecond=0,
    )
    return max(0.0, (end - current).total_seconds())


def save_maintenance_status(reason=None):
    lines = [
        "===== TOSS監視状態 =====",
        "メンテナンス中です。",
    ]
    if reason:
        lines.append(reason)
    else:
        lines.append(
            "夜間停止時間（01:00～07:00）のため監視を停止しています。"
        )
    save_result("\n".join(lines))


def trace(message):
    line = f"[{now()}] [HTTP] {message}"
    print(line)
    with TRACE_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_result(text):
    RESULT_FILE.write_text(text, encoding="utf-8-sig")


def now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")


def _read_runtime_file(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime JSONの最上位がオブジェクトではありません。")
    return payload


def load_runtime():
    for path in (RUNTIME_FILE, RUNTIME_BACKUP_FILE):
        if not path.exists():
            continue
        try:
            return _read_runtime_file(path)
        except Exception:
            continue
    return {}


def save_runtime(patch):
    """LINE側が監視状態・エラー・最終成功時刻を確認できる実行状態を安全に保存する。"""
    payload = load_runtime()
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("version", 1)
    payload.update(patch)
    payload["updated_at"] = now_iso()

    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    _ = _read_runtime_file_from_text(serialized)

    if RUNTIME_FILE.exists():
        try:
            old_text = RUNTIME_FILE.read_text(encoding="utf-8")
            _read_runtime_file_from_text(old_text)
            _write_text_atomic(RUNTIME_BACKUP_FILE, old_text)
        except Exception:
            # runtimeのバックアップ失敗はTOSS状態ファイル保存を妨げない。
            pass
    _write_text_atomic(RUNTIME_FILE, serialized)


def _read_runtime_file_from_text(text):
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("runtime JSONの最上位がオブジェクトではありません。")
    return payload


class StateFileError(RuntimeError):
    """状態ファイル本体とバックアップの両方を正常に読めない場合。"""


def _read_valid_state(path):
    """指定ファイルを読み込み、最低限必要な状態構造を検証する。"""
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)

    if not isinstance(payload, dict):
        raise ValueError("JSONの最上位がオブジェクトではありません。")
    if not isinstance(payload.get("statuses"), dict):
        raise ValueError("statuses が辞書形式ではありません。")

    return payload


def load_state():
    """最新状態を読む。本体が壊れていれば .bak を使用する。"""
    candidates = [STATE_FILE, STATE_BACKUP_FILE]
    existing = [path for path in candidates if path.exists()]

    if not existing:
        trace("状態ファイルがまだ存在しません。今回を初回監視として扱います。")
        return None

    errors = []

    for path in candidates:
        if not path.exists():
            continue

        try:
            payload = _read_valid_state(path)
            if path == STATE_FILE:
                trace("状態ファイル本体を正常に読み込みました。")
            else:
                trace(
                    "状態ファイル本体を読み込めなかったため、"
                    "バックアップ toss_state.json.bak を使用します。"
                )
            return payload
        except Exception as exc:
            errors.append(
                f"{path.name}: {type(exc).__name__}: {exc}"
            )
            trace(
                f"状態ファイル読込エラー: {path.name} | "
                f"{type(exc).__name__}: {exc}"
            )

    raise StateFileError(
        "状態ファイル本体とバックアップのどちらも正常に読めません。 "
        + " / ".join(errors)
    )


def _read_valid_state_from_text(text):
    """文字列として与えられたJSONを最低限検証する。"""
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("JSONの最上位がオブジェクトではありません。")
    if not isinstance(payload.get("statuses"), dict):
        raise ValueError("statuses が辞書形式ではありません。")
    return payload


def _write_text_atomic(path, text):
    """同一フォルダ内の一時ファイルから原子的に置換する。"""
    temp_path = path.with_name(
        f".{path.name}.tmp-{os.getpid()}"
    )

    try:
        with temp_path.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def save_state(statuses, maintenance_dates=None, system_windows=None):
    """最新状態を安全に保存し、直前の正常状態を .bak に残す。"""
    payload = {
        "saved_at": now(),
        "statuses": statuses,
        "maintenance_dates": [
            d.isoformat() for d in sorted(maintenance_dates or set())
        ],
        "system_maintenance_windows": [
            {"start": start.isoformat(), "end": end.isoformat()}
            for start, end in sorted(system_windows or [])
        ],
    }

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )

    # 保存するJSON自体を先に再パースして検証する。
    _ = _read_valid_state_from_text(serialized)

    # 現在の本体が正常なら、その「直前の状態」をまずバックアップする。
    # 本体が壊れている場合でも、既存のバックアップが正常ならそれを保護したまま
    # 新しい完全取得結果で本体を修復できるようにする。
    if STATE_FILE.exists():
        try:
            current_text = STATE_FILE.read_text(encoding="utf-8")
            _read_valid_state_from_text(current_text)
        except Exception as exc:
            trace(
                "現在の状態ファイルは破損しています。バックアップを保護して保存します。 "
                f"{type(exc).__name__}: {exc}"
            )
            try:
                if STATE_BACKUP_FILE.exists():
                    _read_valid_state(STATE_BACKUP_FILE)
                    trace("既存の toss_state.json.bak は正常なので上書きしません。")
                else:
                    raise StateFileError(
                        "状態本体が破損し、利用できるバックアップもありません。"
                    )
            except StateFileError:
                raise
            except Exception as backup_exc:
                raise StateFileError(
                    "状態本体とバックアップの両方に問題があるため、"
                    "安全のため状態ファイルを更新しません。"
                ) from backup_exc
        else:
            try:
                _write_text_atomic(STATE_BACKUP_FILE, current_text)
                trace("直前の正常な状態を toss_state.json.bak に退避しました。")
            except Exception as exc:
                trace(
                    "現在の状態ファイルをバックアップへ退避できませんでした。 "
                    f"{type(exc).__name__}: {exc}"
                )
                raise StateFileError(
                    "状態保存前のバックアップ作成に失敗したため、"
                    "安全のため状態ファイルを更新しません。"
                ) from exc

    # 初回保存でバックアップがまだ無い場合は、今回の完全な状態をバックアップにも残す。
    # 以後の保存では「直前の正常状態」が .bak に残る。
    if not STATE_BACKUP_FILE.exists():
        try:
            _write_text_atomic(STATE_BACKUP_FILE, serialized)
            trace("初回の正常な状態を toss_state.json.bak にも保存しました。")
        except Exception as exc:
            trace(
                "初回状態のバックアップ作成に失敗しました。 "
                f"{type(exc).__name__}: {exc}"
            )
            raise StateFileError(
                "バックアップを作成できなかったため、状態ファイルを更新しません。"
            ) from exc

    _write_text_atomic(STATE_FILE, serialized)
    trace("最新状態を原子的に toss_state.json へ保存しました。")


def load_cached_maintenance(payload):
    """前回正常に取得できたメンテナンス情報を復元する。"""
    if not isinstance(payload, dict):
        return set(), []

    maintenance_dates = set()
    for value in payload.get("maintenance_dates", []):
        try:
            maintenance_dates.add(datetime.strptime(value, "%Y-%m-%d").date())
        except (TypeError, ValueError):
            continue

    windows = []
    for item in payload.get("system_maintenance_windows", []):
        if not isinstance(item, dict):
            continue
        try:
            start = datetime.fromisoformat(item["start"]).astimezone(JST)
            end = datetime.fromisoformat(item["end"]).astimezone(JST)
            if end > start:
                windows.append((start, end))
        except (KeyError, TypeError, ValueError):
            continue

    return maintenance_dates, merge_windows(windows) if windows else []


def normalize_text(value):
    return "".join((value or "").split())


def normalize_notice_text(value):
    """お知らせ本文を比較しやすい形に正規化する。

    NFKCで全角/半角差を吸収し、HTML由来の改行・空白だけを取り除く。
    文中の句読点などは残して、日付・時刻の意味を壊さない。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))


PERIODIC_NOTICE_KEY = "TOSS定期メンテナンス"
SYSTEM_NOTICE_KEY = "システムメンテナンス"
UNAVAILABILITY_KEYWORDS = (
    "本システムを停止",
    "本システム停止",
    "本システムをご利用いただけません",
    "本システムをご利用できません",
    "本システムが利用できません",
    "本システムは利用できません",
    "TOSSを停止",
    "TOSS停止",
    "TOSSをご利用いただけません",
    "TOSSをご利用できません",
    "TOSSが利用できません",
    "TOSSは利用できません",
)

ERA_BASE_YEARS = {
    "令和": 2018,
    "平成": 1988,
    "昭和": 1925,
}

DATE_RE = re.compile(
    r"(?:(令和|平成|昭和)\s*(元|[0-9]{1,2})\s*年\s*)?"
    r"(20[0-9]{2})?\s*[年/.-]?\s*"
    r"(1[0-2]|0?[1-9])\s*[月/.?-]\s*"
    r"(3[01]|[12][0-9]|0?[1-9])\s*日?"
)

FISCAL_YEAR_RE = re.compile(
    r"(?:(令和|平成|昭和)\s*(元|[0-9]{1,2})\s*年度)"
)

SLASH_DATE_RE = re.compile(
    r"(1[0-2]|0?[1-9])\s*[/／]\s*(3[01]|[12][0-9]|0?[1-9])"
)


def _parse_date_match(match, reference_year):
    era = match.group(1)
    era_year = match.group(2)
    western_year = match.group(3)
    month = int(match.group(4))
    day = int(match.group(5))

    if era:
        year = ERA_BASE_YEARS[era] + (1 if era_year == "元" else int(era_year))
    elif western_year:
        year = int(western_year)
    else:
        year = reference_year

    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _parse_fiscal_start_year(text, fallback_year):
    """令和8年度などの年度表記から年度開始年(4月側の西暦年)を求める。"""
    m = FISCAL_YEAR_RE.search(text)
    if not m:
        return fallback_year

    era, era_year = m.groups()
    year_no = 1 if era_year == "元" else int(era_year)
    return ERA_BASE_YEARS[era] + year_no


def is_periodic_toss_maintenance_notice(text):
    """TOSS本体の定期メンテナンス告知だけを判定する。"""
    normalized = normalize_notice_text(text)
    return (
        PERIODIC_NOTICE_KEY in normalized
        and "実施日" in normalized
    )


def is_toss_system_maintenance_notice(text):
    """TOSS本体が利用できなくなるシステムメンテナンス告知だけを判定する。

    体育館・施設個別の「システムメンテナンス」は対象にしない。
    「システムメンテナンス」だけでは不十分で、TOSS本体の停止/利用不可を
    示す文言も同じ告知本文内に存在することを必須にする。
    """
    normalized = normalize_notice_text(text)
    if SYSTEM_NOTICE_KEY not in normalized:
        return False

    mentions_toss = "TOSS" in normalized or "本システム" in normalized
    unavailable = any(keyword in normalized for keyword in UNAVAILABILITY_KEYWORDS)
    return mentions_toss and unavailable


def extract_periodic_maintenance_dates(text, reference_year):
    """「TOSS定期メンテナンス実施日」の日付リストだけを解釈する。"""
    normalized = normalize_notice_text(text)
    if not is_periodic_toss_maintenance_notice(normalized):
        return set()

    # 「TOSS定期メンテナンス実施日」から、その告知文の先頭文程度だけを対象にする。
    # ホームの別コンテンツにある日付を巻き込まない。
    start = normalized.find(PERIODIC_NOTICE_KEY)
    tail = normalized[start:start + 500]
    sentence_end = re.search(r"。", tail)
    if sentence_end:
        tail = tail[:sentence_end.end()]

    fiscal_start_year = _parse_fiscal_start_year(tail, reference_year)
    dates = set()
    for m in SLASH_DATE_RE.finditer(tail):
        month = int(m.group(1))
        day = int(m.group(2))
        year = fiscal_start_year if month >= 4 else fiscal_start_year + 1
        try:
            dates.add(datetime(year, month, day).date())
        except ValueError:
            pass
    return dates


def _valid_clock(hour, minute):
    return 0 <= hour <= 28 and 0 <= minute < 60


def extract_system_maintenance_windows(text, reference_year):
    """TOSS本体を停止するシステムメンテナンスの時間帯だけを解釈する。"""
    normalized = normalize_notice_text(text)
    if not is_toss_system_maintenance_notice(normalized):
        return []

    # 例:
    # 2026年9月29日(火)24:00～28:00（9月30日(水)0:00～4:00）に本システムを停止
    # 「日付＋時刻レンジ」を拾い、停止/利用不可の文脈が近くにあるものだけ採用する。
    range_re = re.compile(
        r"(?:(20[0-9]{2})\s*年\s*)?"
        r"(1[0-2]|0?[1-9])\s*月\s*(3[01]|[12][0-9]|0?[1-9])\s*日"
        r"[^0-9]{0,50}?"
        r"(\d{1,2}):(\d{2})\s*[～〜~\-－]\s*(\d{1,2}):(\d{2})"
    )

    windows = []
    for m in range_re.finditer(normalized):
        before = normalized[max(0, m.start() - 120):m.start()]
        after = normalized[m.end():m.end() + 160]
        context = before + after
        if not any(word in context for word in ("停止", "利用いただけません", "利用できません")):
            continue

        year = int(m.group(1)) if m.group(1) else reference_year
        month = int(m.group(2))
        day = int(m.group(3))
        sh, sm = int(m.group(4)), int(m.group(5))
        eh, em = int(m.group(6)), int(m.group(7))

        if not (_valid_clock(sh, sm) and _valid_clock(eh, em)):
            continue

        try:
            base = datetime(year, month, day, tzinfo=JST)
            start_dt = base + timedelta(hours=sh, minutes=sm)
            end_dt = base + timedelta(hours=eh, minutes=em)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            windows.append((start_dt, end_dt))
        except ValueError:
            continue

    # 日本語告知では「2026年9月29日24:00～28:00」の後ろに
    # 「9月30日0:00～4:00」と明記されることがあるため、明示された通常時刻も候補にする。
    explicit_re = re.compile(
        r"(?:（|\()\s*"
        r"(?:(20[0-9]{2})\s*年\s*)?"
        r"(1[0-2]|0?[1-9])\s*月\s*(3[01]|[12][0-9]|0?[1-9])\s*日"
        r"[^0-9]{0,20}?"
        r"(\d{1,2}):(\d{2})\s*[～〜~\-－]\s*(\d{1,2}):(\d{2})"
    )
    for m in explicit_re.finditer(normalized):
        before = normalized[max(0, m.start() - 120):m.start()]
        after = normalized[m.end():m.end() + 160]
        context = before + after
        if not any(word in context for word in ("停止", "利用いただけません", "利用できません")):
            continue

        year = int(m.group(1)) if m.group(1) else reference_year
        month = int(m.group(2))
        day = int(m.group(3))
        sh, sm = int(m.group(4)), int(m.group(5))
        eh, em = int(m.group(6)), int(m.group(7))
        if sh > 23 or eh > 23 or sm > 59 or em > 59:
            continue

        try:
            start_dt = datetime(year, month, day, sh, sm, tzinfo=JST)
            end_dt = datetime(year, month, day, eh, em, tzinfo=JST)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            windows.append((start_dt, end_dt))
        except ValueError:
            continue

    unique = []
    seen = set()
    for start_dt, end_dt in windows:
        key = (start_dt, end_dt)
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def merge_windows(windows):
    """重なっているシステムメンテナンス時間帯を統合する。"""
    ordered = sorted(windows, key=lambda x: (x[0], x[1]))
    merged = []
    for start_dt, end_dt in ordered:
        if not merged or start_dt > merged[-1][1]:
            merged.append([start_dt, end_dt])
        else:
            merged[-1][1] = max(merged[-1][1], end_dt)
    return [(x[0], x[1]) for x in merged]


def current_system_maintenance_window(windows, dt=None):
    current = dt or datetime.now(JST)
    for start_dt, end_dt in windows:
        if start_dt <= current < end_dt:
            return start_dt, end_dt
    return None


def _notice_entries_from_frame(frame):
    """指定フレームからメンテナンス告知候補を直接拾う。

    お知らせ見出しの祖先構造を前提にしすぎず、告知キーワードを持つ要素を
    DOMから探す。取得した本文は後段で「TOSS本体停止」条件に絞り込むため、
    体育館個別のメンテナンス情報が混在していても誤って停止扱いにはしない。
    """
    return frame.evaluate(
        r"""
        () => {
            const normalize = s => (s || '').normalize('NFKC').replace(/\s/g, '');
            const targets = ['TOSS定期メンテナンス', 'システムメンテナンス'];
            const selectors = [
                'a','li','tr','article','section','h1','h2','h3','h4','h5','h6',
                'dt','dd','p','td','th','strong','b','span'
            ];
            const candidates = [...document.querySelectorAll(selectors.join(','))];
            const out = [];
            const seen = new Set();

            for (const el of candidates) {
                const raw = (el.innerText || el.textContent || '').trim();
                if (!raw || raw.length > 3000) continue;
                const norm = normalize(raw);
                const matched = targets.filter(k => norm.includes(k));
                if (!matched.length) continue;

                let chosen = el;
                // タイトルだけの短い要素なら、直近の親に本文があるか確認する。
                if (norm.length < 60) {
                    let node = el.parentElement;
                    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                        const parentRaw = (node.innerText || node.textContent || '').trim();
                        if (!parentRaw || parentRaw.length > 5000) continue;
                        const parentNorm = normalize(parentRaw);
                        if (matched.some(k => parentNorm.includes(k))) {
                            chosen = node;
                            break;
                        }
                    }
                }

                const text = (chosen.innerText || chosen.textContent || '').trim();
                if (!text) continue;

                const link = chosen.matches('a[href]')
                    ? chosen
                    : chosen.querySelector('a[href]') || el.closest('a[href]');
                const href = link && link.href ? link.href : '';
                const title = link ? (link.getAttribute('title') || '') : '';
                const aria = link ? (link.getAttribute('aria-label') || '') : '';
                const textNorm = normalize(text + title + aria);
                const key = textNorm + '|' + href;
                if (seen.has(key)) continue;
                seen.add(key);

                out.push({
                    text,
                    href,
                    title,
                    aria,
                    matched: targets.filter(k => textNorm.includes(k)),
                });
            }
            return out;
        }
        """
    )


def _notice_entries(page):
    """ページ内の全フレームからメンテナンス告知候補を取得する。"""
    all_entries = []
    for frame in page.frames:
        try:
            all_entries.extend(_notice_entries_from_frame(frame))
        except Exception as exc:
            trace(
                f"お知らせDOM読込エラー: {type(exc).__name__}: {exc}"
            )

    unique = []
    seen = set()
    for entry in all_entries:
        key = (entry.get('text', ''), entry.get('href', ''))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


def _extract_maintenance_notice_text_from_frame(frame):
    """詳細ページからメンテナンス告知本文らしい最小ブロックを取得する。"""
    return frame.evaluate(
        r"""
        () => {
            const normalize = s => (s || '').normalize('NFKC').replace(/\s/g, '');
            const periodic = 'TOSS定期メンテナンス';
            const system = 'システムメンテナンス';
            const stopWords = [
                '本システムを停止', '本システム停止',
                '本システムをご利用いただけません',
                '本システムをご利用できません',
                '本システムが利用できません',
                '本システムは利用できません',
                'TOSSを停止', 'TOSS停止',
                'TOSSをご利用いただけません', 'TOSSをご利用できません',
                'TOSSが利用できません', 'TOSSは利用できません'
            ];

            const blocks = [
                ...document.querySelectorAll(
                    'article,section,li,tr,td,dd,main,div,p,h1,h2,h3,h4,h5,h6,strong,b'
                )
            ];
            let best = '';
            let bestLen = Infinity;

            for (const el of blocks) {
                const raw = (el.innerText || '').trim();
                if (!raw || raw.length > 12000) continue;
                const t = normalize(raw);
                const hasPeriodic =
                    t.includes(periodic) &&
                    t.includes('実施日') &&
                    (/\d{1,2}[\/／]\d{1,2}/.test(t) || /\d{1,2}月\d{1,2}日/.test(t));
                const hasSystem =
                    t.includes(system) &&
                    (t.includes('TOSS') || t.includes('本システム')) &&
                    stopWords.some(w => t.includes(w));
                if (!(hasPeriodic || hasSystem)) continue;

                if (raw.length < bestLen) {
                    best = raw;
                    bestLen = raw.length;
                }
            }
            return best;
        }
        """
    )


def _extract_maintenance_notice_text(page):
    for frame in page.frames:
        try:
            text = _extract_maintenance_notice_text_from_frame(frame)
            if text:
                return text
        except Exception:
            continue
    return ''


def _entry_text_is_body_rich(text):
    """ホームのお知らせリンク自身に本文が含まれているかを判定する。"""
    normalized = normalize_notice_text(text)
    has_date = bool(
        re.search(r'20[0-9]{2}年\s*(?:1[0-2]|0?[1-9])月\s*(?:3[01]|[12][0-9]|0?[1-9])日', normalized)
        or re.search(r'(?:1[0-2]|0?[1-9])月(?:3[01]|[12][0-9]|0?[1-9])日', normalized)
        or re.search(r'(?:1[0-2]|0?[1-9])[／/]\s*(?:3[01]|[12][0-9]|0?[1-9])', normalized)
    )
    has_time = bool(re.search(r'\d{1,2}:\d{2}', normalized))
    has_stop = any(k in normalized for k in UNAVAILABILITY_KEYWORDS) or '終日' in normalized
    return has_date or has_time or has_stop


def read_maintenance_notices(page, today):
    """「お知らせ」からTOSS本体のメンテナンス情報だけを自動解釈する。

    1) TOSS定期メンテナンス実施日 → 記載日は終日利用不可
    2) TOSS本体のシステムメンテナンス → 告知記載の停止時間のみ利用不可

    体育館・施設個別のお知らせやホーム画面の別日付は無視する。
    お知らせ欄を読めない場合は監視を止めず、明示的なエラーとして返す。
    """
    home_return_url = page.url
    maintenance_dates = set()
    system_windows = []
    notice_errors = []
    home_entries = _notice_entries(page)

    if not home_entries:
        # この場合だけ「告知なし」と「お知らせ欄自体が読めない」を区別する必要がある。
        # DOM上にお知らせ見出しが存在するかを確認する。
        heading_exists = False
        for frame in page.frames:
            try:
                heading_exists = bool(frame.evaluate(
                    r"""() => [...document.querySelectorAll('*')].some(el => {
                        const t = (el.innerText || el.textContent || '')
                            .normalize('NFKC').replace(/\s/g, '');
                        return t === 'お知らせ' || (t.includes('お知らせ') && t.length <= 30);
                    })"""
                ))
            except Exception:
                continue
            if heading_exists:
                break

        if heading_exists:
            trace('お知らせ確認: メンテナンス告知候補は見つかりませんでした。')
        else:
            notice_errors.append('ホームのお知らせ欄を読み取れませんでした。')
            trace('お知らせエラー: ホームのお知らせ欄を読み取れませんでした。')
        return maintenance_dates, system_windows, notice_errors

    detail_hrefs = []
    seen_hrefs = set()

    def parse_notice(text, source_label):
        normalized = normalize_notice_text(text or '')
        if not normalized:
            return False, False

        parsed_any = False
        recognized_as_toss_notice = False

        if is_periodic_toss_maintenance_notice(normalized):
            recognized_as_toss_notice = True
            found = extract_periodic_maintenance_dates(normalized, today.year)
            if found:
                maintenance_dates.update(found)
                parsed_any = True
                trace(
                    f"{source_label}: TOSS定期メンテナンス日="
                    + ', '.join(d.isoformat() for d in sorted(found))
                )
            else:
                notice_errors.append(
                    f'{source_label}: TOSS定期メンテナンスの実施日を読み取れませんでした。'
                )

        if is_toss_system_maintenance_notice(normalized):
            recognized_as_toss_notice = True
            found = extract_system_maintenance_windows(normalized, today.year)
            if found:
                system_windows.extend(found)
                parsed_any = True
                trace(
                    f"{source_label}: TOSS本体システムメンテナンス="
                    + ', '.join(
                        f"{a:%Y-%m-%d %H:%M}～{b:%Y-%m-%d %H:%M}"
                        for a, b in found
                    )
                )
            else:
                notice_errors.append(
                    f'{source_label}: TOSS本体システムメンテナンスの停止時間を読み取れませんでした。'
                )

        return parsed_any, recognized_as_toss_notice

    base_netloc = urlsplit(BASE_URL).netloc
    for entry in home_entries:
        entry_text = ' '.join(
            x for x in [entry.get('text'), entry.get('title'), entry.get('aria')] if x
        )

        # タイトルだけの短いリンクは、詳細本文を読み取る。
        # 本文が含まれている場合だけ、ここで先に解析する。
        if _entry_text_is_body_rich(entry_text):
            parse_notice(entry_text, 'お知らせ欄')

        href = entry.get('href') or ''
        if not href or href in seen_hrefs:
            continue
        parsed = urlsplit(href)
        if parsed.scheme not in ('http', 'https') or parsed.netloc != base_netloc:
            continue
        seen_hrefs.add(href)
        detail_hrefs.append(href)

    for href in detail_hrefs:
        try:
            ensure_not_maintenance()
            page.goto(href, wait_until='domcontentloaded', timeout=20000)
            detail_text = _extract_maintenance_notice_text(page)
            if detail_text:
                _, recognized = parse_notice(detail_text, 'お知らせ詳細')
                if not recognized:
                    # 施設個別の「システムメンテナンス」なら対象外。
                    trace('お知らせ詳細: TOSS本体停止条件に該当しないため無視します。')
            else:
                # ホームでTOSS定期/システムメンテナンス候補だったため、
                # 詳細本文を読めないこと自体は改善対象として明示する。
                entry_hint = href
                notice_errors.append(
                    f'お知らせ詳細を読み取れませんでした: {entry_hint}'
                )
                trace(f'お知らせ詳細エラー: 本文を読み取れませんでした: {entry_hint}')
        except Exception as exc:
            notice_errors.append(
                f"お知らせ詳細の取得に失敗しました: {type(exc).__name__}: {exc}"
            )
            trace(
                f"お知らせ詳細取得エラー: {type(exc).__name__}: {exc}"
            )
        finally:
            try:
                page.goto(home_return_url, wait_until='domcontentloaded', timeout=20000)
            except Exception as exc:
                raise RuntimeError(
                    f"お知らせ確認後にホームへ戻れませんでした: {type(exc).__name__}: {exc}"
                ) from exc

    system_windows = merge_windows(system_windows)
    log_end = today + timedelta(days=DAYS_AHEAD)
    shown_dates = sorted(d for d in maintenance_dates if today <= d <= log_end)
    shown_windows = [
        (a, b) for a, b in system_windows
        if b >= datetime.combine(today, datetime.min.time(), JST)
        and a <= datetime.combine(log_end, datetime.max.time(), JST)
    ]

    trace(
        'お知らせ確認: '
        f"監視期間内の終日メンテナンス日={', '.join(d.isoformat() for d in shown_dates) or 'なし'}"
    )
    trace(
        'お知らせ確認: '
        '監視期間内のTOSS本体システムメンテナンス='
        + (
            ', '.join(
                f"{a:%Y-%m-%d %H:%M}～{b:%Y-%m-%d %H:%M}"
                for a, b in shown_windows
            )
            if shown_windows else 'なし'
        )
    )
    if notice_errors:
        trace('お知らせ確認エラー: ' + ' / '.join(notice_errors))

    return maintenance_dates, system_windows, notice_errors

def seconds_until_next_day():
    current = datetime.now(JST)
    next_day = (current + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(0.0, (next_day - current).total_seconds())


def checkbox_label_text(cb):
    return cb.evaluate(
        """
        e => {
            const norm = s => (s || '').replace(/\\s/g, '');

            const label = e.closest('label');
            if (label) return norm(label.innerText || '');

            if (e.id) {
                const forLabel = document.querySelector(
                    `label[for="${CSS.escape(e.id)}"]`
                );
                if (forLabel) return norm(forLabel.innerText || '');
            }

            const parent = e.parentElement;
            if (parent) {
                for (const child of [...parent.children]) {
                    if (child === e) continue;
                    const t = norm(child.innerText || child.textContent || '');
                    if (t) return t;
                }
            }

            return '';
        }
        """
    )


def find_checkbox(page, keyword):
    target = normalize_text(keyword)
    cbs = page.locator("input[type='checkbox']")

    for i in range(cbs.count()):
        cb = cbs.nth(i)
        try:
            if not cb.is_visible():
                continue
            if checkbox_label_text(cb) == target:
                return cb
        except Exception:
            continue

    raise RuntimeError(
        f"「{keyword}」のチェックボックスを完全一致で特定できませんでした。"
    )


def click_purpose(page):
    """目的検索画面へ移動する。通常の画面クリックを優先し、失敗時は同じ公開GET画面へ移動する。"""
    candidates = page.locator("a, input, button, td, span, div, img")
    found = []

    # まず通常の「目的から」操作を試す。
    for i in range(candidates.count()):
        try:
            el = candidates.nth(i)
            if not el.is_visible():
                continue

            values = {
                normalize_text(el.inner_text()),
                normalize_text(el.get_attribute("value")),
                normalize_text(el.get_attribute("alt")),
                normalize_text(el.get_attribute("title")),
            }
            if "目的から" in values:
                found.append(el)
        except Exception:
            continue

    for el in reversed(found):
        try:
            el.click(force=True)
            page.wait_for_timeout(2500)
            if "rsvPurposeSearch.html" in page.url:
                return
        except Exception:
            continue

    # クリックが安定しない場合は、今回の通信ログで確認済みの
    # 通常の目的検索画面URLへGETする。フォーム送信ではない。
    purpose_url = f"{BASE_URL}/TOSS/web/view/user/rsvPurposeSearch.html"
    trace("目的検索画面への通常GETへ切り替えます。")
    page.goto(purpose_url, wait_until="domcontentloaded", timeout=30000)

    try:
        page.locator("input[type='checkbox']").first.wait_for(
            state="attached",
            timeout=15000,
        )
    except Exception as exc:
        raise RuntimeError(
            f"目的検索画面の読み込みを確認できませんでした: {type(exc).__name__}: {exc}"
        ) from exc

    # te-uniquekey のクエリ文字列は正常なセッション識別情報なので、
    # URL全体ではなくパスだけを確認する。
    actual_path = urlsplit(page.url).path
    expected_path = "/TOSS/web/view/user/c005RsvPurposeSearch.html"
    if actual_path != expected_path:
        raise RuntimeError(
            f"目的検索画面への遷移後パスが想定外です: {actual_path!r} "
            f"(URL={page.url})"
        )


def set_purposes(page):
    for keyword in PURPOSES:
        cb = find_checkbox(page, keyword)
        if not cb.is_checked():
            cb.check(force=True)

    volleyball = find_checkbox(page, "バレーボール")
    if volleyball.is_checked():
        volleyball.uncheck(force=True)

    checked = []
    cbs = page.locator("input[type='checkbox']:checked")
    for i in range(cbs.count()):
        checked.append(checkbox_label_text(cbs.nth(i)))

    normalized = {normalize_text(x) for x in checked if x}
    expected = {normalize_text(x) for x in PURPOSES}

    if normalized != expected:
        raise RuntimeError(
            f"検索条件の確認に失敗しました。選択={checked}"
        )


def wait_for_complete_search_results(page, timeout_ms=15000):
    """検索結果1ページ分のDOM描画完了を確認する。

    最初のemptyStateIconだけが先に描画される場合があるため、
    5カード・各3時間帯（計15枠）が揃うまで待ってからHTMLを取得する。
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    last = None

    while True:
        last = page.evaluate(
            """
            () => {
                const cards = [...document.querySelectorAll('table.tablebg2')]
                    .filter(table =>
                        table.querySelector('span#bnamem') &&
                        table.querySelector('span#inamem')
                    );

                return {
                    all_count: document.querySelector(
                        "input[name='layoutChildBody:childForm:allCount']"
                    )?.value || '',
                    card_count: cards.length,
                    icon_count: document.querySelectorAll('img#emptyStateIcon').length,
                    tzoneno_count: document.querySelectorAll('input[id="tzoneno"]').length,
                    card_slot_counts: cards.map(table => ({
                        icons: table.querySelectorAll('img#emptyStateIcon').length,
                        tzoneno: table.querySelectorAll('input[id="tzoneno"]').length
                    }))
                };
            }
            """
        )

        if (
            last["all_count"] == "63"
            and last["card_count"] == 5
            and last["icon_count"] == 15
            and last["tzoneno_count"] == 15
            and all(
                x["icons"] == 3 and x["tzoneno"] == 3
                for x in last["card_slot_counts"]
            )
        ):
            return

        if time.monotonic() >= deadline:
            raise RuntimeError(
                "検索結果の描画完了を確認できませんでした: "
                f"allCount={last['all_count']}, "
                f"cards={last['card_count']}, "
                f"icons={last['icon_count']}, "
                f"tzoneno={last['tzoneno_count']}"
            )

        page.wait_for_timeout(250)


def click_search(page):
    elements = page.locator("input, button, a")
    candidates = []

    for i in range(elements.count()):
        try:
            el = elements.nth(i)
            blob = "|".join(
                [
                    normalize_text(el.inner_text()),
                    normalize_text(el.get_attribute("value")),
                    normalize_text(el.get_attribute("onclick")),
                ]
            )
            if "上記の内容で検索する" in blob:
                candidates.append(el)
        except Exception:
            continue

    if not candidates:
        raise RuntimeError("「上記の内容で検索する」が見つかりませんでした。")

    for el in reversed(candidates):
        try:
            el.click(force=True)
            wait_for_complete_search_results(page)
            return
        except Exception:
            continue

    raise RuntimeError("検索結果画面への移動または描画完了を確認できませんでした。")



def raise_maintenance_if_current_window(system_windows, exc):
    """現在が告知済みメンテナンス時間帯で、実際のアクセスに失敗した場合だけメンテナンス扱いにする。"""
    current_window = current_system_maintenance_window(system_windows)
    if not current_window:
        raise exc

    start_dt, end_dt = current_window
    reason = (
        f"TOSS本体への実アクセスが失敗し、告知済みのシステムメンテナンス時間帯 "
        f"（{start_dt:%Y-%m-%d %H:%M}～{end_dt:%Y-%m-%d %H:%M}）とも一致したため、"
        "メンテナンス中と判定して監視を停止します。"
    )
    return ScheduledSystemMaintenanceError(reason, end_dt)

def bootstrap_browser_session(playwright):
    """
    ブラウザでは通常の検索条件確立だけを行う。
    その後は同じBrowserContextに紐づくHTTP APIへ切り替える。
    """
    trace("HTTP監視用のセッションをブラウザで1回だけ確立します。")

    browser = playwright.chromium.launch(headless=HEADLESS)
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()

    try:
        ensure_not_maintenance()
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            cached_dates, cached_windows = load_cached_maintenance(load_state())
            maintenance_error = raise_maintenance_if_current_window(
                cached_windows,
                RuntimeError(f"TOSSホーム画面へのアクセスに失敗しました: {type(exc).__name__}: {exc}"),
            )
            if isinstance(maintenance_error, ScheduledSystemMaintenanceError):
                raise maintenance_error from exc
            raise
        trace(f"ブラウザ初期表示: {page.url}")

        ensure_not_maintenance()
        today = datetime.now(JST).date()
        maintenance_dates, system_windows, notice_errors = read_maintenance_notices(page, today)
        if today in maintenance_dates:
            reason = (
                f"お知らせで本日（{today.isoformat()}）がTOSS定期メンテナンス実施日と確認されました。"
            )
            save_maintenance_status(reason)
            raise ScheduledMaintenanceError(reason)

        # システムメンテナンスは「告知がある」だけでは停止しない。
        # 実際にTOSSの目的検索・空き状況画面まで開けるかを試し、失敗した場合のみ
        # 現在の告知時間帯と照合してメンテナンス中と判定する。
        ensure_not_maintenance()
        try:
            click_purpose(page)
            set_purposes(page)
            ensure_not_maintenance()
            click_search(page)
        except Exception as exc:
            maintenance_error = raise_maintenance_if_current_window(
                system_windows,
                RuntimeError(f"TOSSの空き状況画面へのアクセスに失敗しました: {type(exc).__name__}: {exc}"),
            )
            if isinstance(maintenance_error, ScheduledSystemMaintenanceError):
                save_maintenance_status(str(maintenance_error))
                raise maintenance_error from exc
            raise

        page.locator("img#emptyStateIcon").first.wait_for(
            state="attached",
            timeout=10000,
        )

        html = page.content()
        current_url = page.url
        user_agent = page.evaluate("navigator.userAgent")
        browser_charset = page.evaluate("document.characterSet || document.charset || ''")

        trace(f"ブラウザ検索完了: {current_url}")
        trace(f"ブラウザが認識した文書文字コード={browser_charset or '取得不可'}")
        trace(f"検索結果HTML長={len(html)}")

        return browser, context, page, html, current_url, user_agent, browser_charset, maintenance_dates, system_windows, notice_errors
    except Exception:
        try:
            browser.close()
        except Exception:
            pass
        raise


def load_html_into_page(page, html):
    """HTTPレスポンスをネットワークアクセスなしでDOM化する。"""
    page.set_content(
        html,
        wait_until="domcontentloaded",
        timeout=10000,
    )


def read_current_date(page):
    year = page.locator("#year--").inner_text().strip()
    month = page.locator("#month--").inner_text().strip()
    day = page.locator("#day--").inner_text().strip()
    text = f"{year}/{month}/{day}"

    try:
        return datetime.strptime(text, "%Y/%m/%d").date()
    except ValueError as exc:
        raise RuntimeError(f"DOMの日付形式が不正です: {text}") from exc


def read_all_count(page):
    el = page.locator(
        "input[name='layoutChildBody:childForm:allCount']"
    )
    if el.count() == 0:
        raise RuntimeError("DOMからallCountを取得できませんでした。")

    try:
        return int(el.first.get_attribute("value") or "")
    except ValueError as exc:
        raise RuntimeError("allCountの値が不正です。") from exc


def extract_form_pairs(page):
    """検索結果フォームの現在の状態をそのまま取得する。"""
    forms = page.locator("form")
    target_form = None

    for i in range(forms.count()):
        form = forms.nth(i)
        if form.locator(
            "input[name='layoutChildBody:childForm:rsvEmptyStateItemsSave']"
        ).count() > 0:
            target_form = form
            break

    if target_form is None:
        raise RuntimeError("空き状況検索用フォームを特定できませんでした。")

    method = (target_form.get_attribute("method") or "get").lower()
    if method != "post":
        raise RuntimeError(
            f"検索結果フォームのmethodがPOSTではありません: {method}"
        )

    action = target_form.get_attribute("action") or ""
    if action:
        if action.startswith(("http://", "https://")):
            if not action.startswith(BASE_URL + EMPTY_STATE_PATH):
                raise RuntimeError(f"許可外のフォーム送信先です: {action}")
        else:
            normalized = action.split("?", 1)[0]
            if not normalized.endswith(EMPTY_STATE_PATH):
                raise RuntimeError(f"許可外のフォーム送信先です: {action}")

    pairs = target_form.evaluate(
        """
        form => {
            const out = [];
            const add = (name, value) => {
                if (name) out.push([name, value ?? '']);
            };

            for (const el of form.querySelectorAll('input[name]')) {
                const name = el.getAttribute('name');
                const type = (el.getAttribute('type') || 'text').toLowerCase();

                if (['submit', 'button', 'image', 'reset', 'file'].includes(type)) {
                    continue;
                }
                if ((type === 'checkbox' || type === 'radio') && !el.checked) {
                    continue;
                }
                add(name, el.value || '');
            }

            for (const el of form.querySelectorAll('select[name]')) {
                for (const option of [...el.options]) {
                    if (option.selected) {
                        add(
                            el.getAttribute('name'),
                            option.value ?? option.textContent ?? ''
                        );
                    }
                }
            }

            for (const el of form.querySelectorAll('textarea[name]')) {
                add(el.getAttribute('name'), el.value || el.textContent || '');
            }

            return out;
        }
        """
    )

    pairs = [(str(name), str(value)) for name, value in pairs]

    if not any(
        name == "layoutChildBody:childForm:rsvEmptyStateItemsSave"
        for name, _ in pairs
    ):
        raise RuntimeError("検索結果フォームの必須状態データが取得できませんでした。")

    return pairs


def build_readonly_post_data(page, action_field, extra_fields):
    if action_field not in ALLOWED_ACTION_FIELDS:
        raise RuntimeError(f"許可されていないHTTP操作です: {action_field}")

    pairs = extract_form_pairs(page)
    pairs = [
        (name, value)
        for name, value in pairs
        if name not in ALLOWED_ACTION_FIELDS
    ]

    data = list(pairs)

    for key, value in extra_fields.items():
        replaced = False
        new_data = []
        for name, old_value in data:
            if name == key:
                if not replaced:
                    new_data.append((name, str(value)))
                    replaced = True
            else:
                new_data.append((name, old_value))
        if not replaced:
            new_data.append((key, str(value)))
        data = new_data

    data.append((action_field, "submit"))
    return data


def validate_readonly_post(url, data):
    if not url.startswith(BASE_URL + EMPTY_STATE_PATH):
        raise RuntimeError(f"HTTP送信先が許可範囲外です: {url}")

    action_names = {name for name, _ in data} & ALLOWED_ACTION_FIELDS
    if len(action_names) != 1:
        raise RuntimeError(
            f"HTTPアクション指定が不正です。検出={sorted(action_names)}"
        )


CHARSET_ALIASES = {
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "shift_jis": "cp932",
    "shift-jis": "cp932",
    "sjis": "cp932",
    "ms932": "cp932",
    "windows-31j": "cp932",
    "windows_31j": "cp932",
    "cp932": "cp932",
    "euc-jp": "euc_jp",
    "euc_jp": "euc_jp",
    "iso-2022-jp": "iso2022_jp",
    "iso2022-jp": "iso2022_jp",
}


def canonical_charset(name):
    value = (name or "").strip().strip("\"'").lower()
    if not value:
        return None
    return CHARSET_ALIASES.get(value, value)


def charset_from_content_type(content_type):
    m = re.search(
        r"charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)",
        content_type or "",
        re.IGNORECASE,
    )
    return canonical_charset(m.group(1)) if m else None


def charset_from_html_meta(raw_body):
    """HTML本文先頭部のASCII互換部分からcharset指定を探す。"""
    probe = raw_body[:65536].decode("latin-1", errors="ignore")
    patterns = (
        r"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)",
        r"<meta[^>]+content\s*=\s*[\"'][^\"']*charset\s*=\s*([A-Za-z0-9._:-]+)",
    )
    for pattern in patterns:
        m = re.search(pattern, probe, re.IGNORECASE)
        if m:
            return canonical_charset(m.group(1))
    return None


def decode_http_html(response, fallback_charset=None):
    """HTTPレスポンスHTMLを実際の文字コードに合わせて安全にデコードする。

    PlaywrightのAPIResponse.text()は現在の実装ではresponse bodyをUTF-8として
    文字列化するため、TOSSがShift_JIS/Windows-31Jを返す場合に備えて
    raw body()を取得し、HTTPヘッダ→BOM→HTML meta→ブラウザ確認値→候補の順で
    文字コードを決める。
    """
    raw = response.body()
    if not raw:
        raise RuntimeError("HTTP応答本文が空です。")

    header_charset = charset_from_content_type(
        response.headers.get("content-type", "")
    )
    meta_charset = charset_from_html_meta(raw)
    browser_charset = canonical_charset(fallback_charset)

    # BOMは他の推測より優先。
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates = ["utf-8-sig"]
    elif raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        candidates = ["utf-16"]
    else:
        candidates = []

    # HTTPヘッダの指定を最優先。ヘッダが無い/不正な場合は、同じページを
    # 実際にブラウザで描画したときのcharacterSetを優先し、meta→一般候補へ進む。
    for enc in (header_charset, browser_charset, meta_charset, "utf-8", "cp932", "euc_jp"):
        enc = canonical_charset(enc)
        if enc and enc not in candidates:
            candidates.append(enc)

    errors = []
    for enc in candidates:
        try:
            return (
                raw.decode(enc, errors="strict"),
                enc,
                header_charset,
                meta_charset,
                len(raw),
            )
        except (UnicodeDecodeError, LookupError) as exc:
            errors.append(f"{enc}:{type(exc).__name__}")

    raise RuntimeError(
        "HTTPレスポンスHTMLをデコードできませんでした。"
        f" Content-Type={response.headers.get('content-type', '')!r};"
        f" 候補={candidates}; 詳細={errors}"
    )


def http_post_readonly(request_context, page, current_html, current_url, user_agent, action_field, extra_fields, system_windows, fallback_charset=None):
    """
    現在のDOMから検索結果フォームを取得し、読み取り専用のページ移動POSTだけ送る。
    現在HTMLをここで再度set_contentしないことで、無駄なDOM再構築を1回減らす。
    """
    data = build_readonly_post_data(page, action_field, extra_fields)
    validate_readonly_post(URL, data)

    action_name = action_field.rsplit(":", 1)[-1]
    trace(
        f"読み取り用HTTP POST: action={action_name}, fields={len(data)}"
    )

    ensure_not_maintenance()
    time.sleep(REQUEST_GAP_SECONDS)
    ensure_not_maintenance()
    encoded = urlencode(data, doseq=True)

    try:
        response = request_context.post(
            URL,
            data=encoded,
            headers={
                "User-Agent": user_agent,
                "Referer": current_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=HTTP_TIMEOUT_MS,
            fail_on_status_code=False,
            max_redirects=5,
            max_retries=0,
        )
    except Exception as exc:
        current_window = current_system_maintenance_window(system_windows)
        if current_window:
            start_dt, end_dt = current_window
            raise ScheduledSystemMaintenanceError(
                f"TOSSへの実アクセスが失敗し、告知済みのシステムメンテナンス時間帯 "
                f"（{start_dt:%Y-%m-%d %H:%M}～{end_dt:%Y-%m-%d %H:%M}）とも一致したため、"
                "メンテナンス中と判定して監視を停止します。",
                end_dt,
            ) from exc
        raise RuntimeError(
            f"HTTP通信に失敗しました: {type(exc).__name__}: {exc}"
        ) from exc

    current_window = current_system_maintenance_window(system_windows)

    if response.status != 200:
        if current_window:
            start_dt, end_dt = current_window
            raise ScheduledSystemMaintenanceError(
                f"TOSSへの実アクセスがHTTP {response.status}で失敗し、告知済みのシステムメンテナンス時間帯 "
                f"（{start_dt:%Y-%m-%d %H:%M}～{end_dt:%Y-%m-%d %H:%M}）とも一致したため、"
                "メンテナンス中と判定して監視を停止します。",
                end_dt,
            )
        raise RuntimeError(
            f"HTTP応答が200ではありません: {response.status} {response.url}"
        )

    if not response.url.startswith(BASE_URL + EMPTY_STATE_PATH):
        if current_window:
            start_dt, end_dt = current_window
            raise ScheduledSystemMaintenanceError(
                f"TOSSへの実アクセス先が想定外となり、告知済みのシステムメンテナンス時間帯 "
                f"（{start_dt:%Y-%m-%d %H:%M}～{end_dt:%Y-%m-%d %H:%M}）とも一致したため、"
                "メンテナンス中と判定して監視を停止します。",
                end_dt,
            )
        raise RuntimeError(f"HTTP応答先が想定外です: {response.url}")

    html, used_charset, header_charset, meta_charset, raw_size = decode_http_html(
        response,
        fallback_charset=fallback_charset,
    )
    global HTTP_ENCODING_TRACE_LOGGED
    if not HTTP_ENCODING_TRACE_LOGGED:
        trace(
            "HTTP応答文字コード確認: "
            f"使用={used_charset}, Content-Type指定={header_charset or 'なし'}, "
            f"HTML meta指定={meta_charset or 'なし'}, "
            f"ブラウザ確認値={canonical_charset(fallback_charset) or 'なし'}, "
            f"生バイト={raw_size}"
        )
        HTTP_ENCODING_TRACE_LOGGED = True
    if "rsvEmptyStateItemsSave" not in html:
        if current_window:
            start_dt, end_dt = current_window
            raise ScheduledSystemMaintenanceError(
                f"TOSSへの実アクセス結果に空き状況画面が返らず、告知済みのシステムメンテナンス時間帯 "
                f"（{start_dt:%Y-%m-%d %H:%M}～{end_dt:%Y-%m-%d %H:%M}）とも一致したため、"
                "メンテナンス中と判定して監視を停止します。",
                end_dt,
            )
        raise RuntimeError("HTTP応答に空き状況検索フォームが見つかりません。")

    # 応答HTMLをDOM化するのは次の読み取り処理に必要になったときだけ。
    load_html_into_page(page, html)
    return html, response.url


def get_current_page_facilities(page):
    """現在ページのカード単位で5件程度を取得する。"""
    return page.evaluate(
        """
        () => {
            const results = [];
            const cardTables = [...document.querySelectorAll('table.tablebg2')]
                .filter(table =>
                    table.querySelector('span#bnamem') &&
                    table.querySelector('span#inamem')
                );

            if (!cardTables.length) {
                throw new Error('検索結果から施設カードを取得できませんでした。');
            }

            for (let i = 0; i < cardTables.length; i++) {
                const table = cardTables[i];
                const facilityEl = table.querySelector('span#bnamem');
                const itemEl = table.querySelector('span#inamem');
                const facilityEls = table.querySelectorAll('span#bnamem');
                const itemEls = table.querySelectorAll('span#inamem');
                const iconEls = [...table.querySelectorAll('img#emptyStateIcon')];
                const tzEls = [...table.querySelectorAll('input[id="tzoneno"]')];

                if (facilityEls.length !== 1 || itemEls.length !== 1) {
                    throw new Error(
                        '結果' + (i + 1) + 'の施設名/区画名構造が不正です: ' +
                        facilityEls.length + '/' + itemEls.length
                    );
                }

                const facility = (facilityEl.textContent || '').trim();
                const item = (itemEl.textContent || '').trim();
                if (!facility || !item) {
                    throw new Error('結果' + (i + 1) + 'の施設名または区画名が空です。');
                }

                if (iconEls.length !== 3 || tzEls.length !== 3) {
                    throw new Error(
                        '結果' + (i + 1) + 'の時間帯要素数が不正です: ' +
                        'tzoneno=' + tzEls.length + ', icon=' + iconEls.length
                    );
                }

                const statuses = [];

                for (let j = 0; j < 3; j++) {
                    const input = tzEls[j];
                    const tzoneno = input.value || '';
                    const td = input.closest('td');
                    const icon = td ? td.querySelector('img#emptyStateIcon') : null;
                    const status = icon ? (icon.getAttribute('alt') || '') : '';

                    if (!tzoneno || !status) {
                        throw new Error(
                            '結果' + (i + 1) + 'の時間帯または状態が空です。'
                        );
                    }

                    statuses.push({tzoneno, status});
                }

                results.push({facility, item, statuses});
            }

            return results;
        }
        """
    )

def scan_one_date_http(request_context, page, html, current_url, user_agent, target_date, system_windows, fallback_charset=None):
    load_html_into_page(page, html)
    current_date = read_current_date(page)

    if current_date != target_date:
        html, current_url = http_post_readonly(
            request_context,
            page,
            html,
            current_url,
            user_agent,
            "layoutChildBody:childForm:doChangeDate",
            {
                "layoutChildBody:childForm:year": target_date.year,
                "layoutChildBody:childForm:month": target_date.month,
                "layoutChildBody:childForm:day": target_date.day,
                "layoutChildBody:childForm:offset": 0,
            },
            system_windows,
            fallback_charset=fallback_charset,
        )

        load_html_into_page(page, html)
        current_date = read_current_date(page)
        if current_date != target_date:
            raise RuntimeError(
                f"日付変更後の日付が不一致です: 期待={target_date}, 実際={current_date}"
            )

    all_count = read_all_count(page)
    if all_count != 63:
        raise RuntimeError(
            f"検索結果件数が想定外です: allCount={all_count}（期待=63）"
        )

    all_facilities = []
    offset = 0
    page_no = 1

    while offset < all_count:
        if read_current_date(page) != target_date:
            raise RuntimeError(
                f"ページ読み込み前の日付が不正です: 期待={target_date}, 実際={read_current_date(page)}"
            )

        current_all_count = read_all_count(page)
        if current_all_count != all_count:
            raise RuntimeError(
                f"ページ途中でallCountが変化しました: {all_count}->{current_all_count}"
            )

        facilities = get_current_page_facilities(page)
        expected_count = min(5, all_count - offset)
        if len(facilities) != expected_count:
            raise RuntimeError(
                f"{target_date} page={page_no}: 件数不一致 "
                f"取得={len(facilities)}, 期待={expected_count}, offset={offset}"
            )

        all_facilities.extend(facilities)
        trace(
            f"{target_date.isoformat()} page={page_no}/13 "
            f"offset={offset} 件数={len(facilities)}"
        )

        offset += len(facilities)
        if offset >= all_count:
            break

        html, current_url = http_post_readonly(
            request_context,
            page,
            html,
            current_url,
            user_agent,
            "layoutChildBody:childForm:doPager",
            {"layoutChildBody:childForm:offset": offset},
            system_windows,
            fallback_charset=fallback_charset,
        )

        next_date = read_current_date(page)
        if next_date != target_date:
            raise RuntimeError(
                f"ページ移動後に日付が変化しました: 期待={target_date}, 実際={next_date}"
            )
        page_no += 1

    if page_no != 13 or len(all_facilities) != 63:
        raise RuntimeError(
            f"{target_date.isoformat()}: 最終件数が不正です "
            f"ページ={page_no}, 件数={len(all_facilities)}"
        )

    actual_statuses = sum(len(x["statuses"]) for x in all_facilities)
    if actual_statuses != 189:
        raise RuntimeError(
            f"{target_date.isoformat()}: ステータス数が不正です "
            f"{actual_statuses}（期待=189）"
        )

    for number, facility in enumerate(all_facilities, start=1):
        facility["card_no"] = number

    return all_facilities, html, current_url


def make_status_snapshot(date_str, facilities):
    statuses = {}
    for facility in facilities:
        card_no = facility["card_no"]
        for st in facility["statuses"]:
            key = f"{date_str}|{card_no}|{st['tzoneno']}"
            statuses[key] = {
                "status": st["status"],
                "facility": facility["facility"],
                "item": facility["item"],
                "card_no": card_no,
                "tzoneno": st["tzoneno"],
                "time_name": TIME_MAP.get(st["tzoneno"], st["tzoneno"]),
            }
    return statuses


def build_current_state(results_by_date):
    current = {}
    for date_str, facilities in results_by_date.items():
        current.update(make_status_snapshot(date_str, facilities))
    return current


def detect_non_available_to_available(previous_payload, current_statuses):
    if not previous_payload:
        return []

    previous = previous_payload.get("statuses", {})
    if not isinstance(previous, dict):
        return []

    changed = []
    for key, current in current_statuses.items():
        if current.get("status") != "空き":
            continue

        old = previous.get(key)
        if not isinstance(old, dict):
            continue

        old_status = old.get("status")
        if old_status == "空き" or not old_status:
            continue

        date_str, card_no_str, tzoneno = key.split("|", 2)
        changed.append({
            "date": date_str,
            "card_no": int(card_no_str),
            "facility": current["facility"],
            "item": current["item"],
            "time_name": current["time_name"],
            "tzoneno": tzoneno,
            "from_status": old_status,
            "to_status": "空き",
        })

    changed.sort(key=lambda x: (x["date"], x["card_no"], x["tzoneno"]))
    return changed


def format_results(results_by_date, changes, errors, notice_errors=None, maintenance_dates=None, range_start=None, range_end=None):
    lines = [
        "===== TOSS空き状況（HTTP監視テスト） =====",
        "",
    ]

    for date_str in sorted(results_by_date):
        lines.append(f"===== {date_str} =====")
        for facility in results_by_date[date_str]:
            lines.append(
                f"{facility['facility']} / "
                f"{facility['item']} {facility['card_no']}"
            )
            for st in facility["statuses"]:
                lines.append(
                    f"  {TIME_MAP.get(st['tzoneno'], st['tzoneno'])}: "
                    f"{STATUS_MAP.get(st['status'], st['status'])}"
                )
            lines.append("")

    lines.append("===== 今回検出した 空き化（○以外→○） =====")
    if changes:
        for change in changes:
            lines.append(
                f"★ {change['date']} / "
                f"{change['facility']} / "
                f"{change['item']} {change['card_no']} / "
                f"{change['time_name']} / "
                f"{STATUS_MAP.get(change['from_status'], change['from_status'])} → ○"
            )
    else:
        lines.append("なし")


    if notice_errors:
        lines.extend(["", "===== お知らせ取得エラー ====="])
        for message in notice_errors:
            lines.append(f"・{message}")

    if maintenance_dates:
        shown = sorted(maintenance_dates)
        if range_start and range_end:
            shown = [d for d in shown if range_start <= d <= range_end]
        if shown:
            lines.extend(["", "===== お知らせによるメンテナンス予定 ====="])
            for d in shown:
                lines.append(f"{d.isoformat()}: TOSS定期メンテナンス（当日は終日利用不可）")

    if errors:
        lines.extend(["", "===== 取得エラー ====="])
        for date_str in sorted(errors):
            lines.append(f"{date_str}: {errors[date_str]}")

    return "\n".join(lines)


def build_target_dates():
    """監視対象日は曜日だけで決める。

    メンテナンス予定日は「予約状況を見ない日」ではなく、
    「その日当日にTOSS本体が利用できない日」なので、未来日も監視対象に含める。
    """
    today = datetime.now(JST).date()
    end_date = today + timedelta(days=DAYS_AHEAD)
    targets = []

    target = today
    while target <= end_date:
        if target.weekday() not in CLOSED_WEEKDAYS:
            targets.append(target)
        else:
            trace(f"{target.isoformat()} は月曜日のためスキップします。")
        target += timedelta(days=1)

    return today, end_date, targets


def scan_once(playwright):
    global HTTP_ENCODING_TRACE_LOGGED
    HTTP_ENCODING_TRACE_LOGGED = False

    today, end_date, all_target_dates = build_target_dates()
    trace(
        f"===== HTTP監視開始: {today.isoformat()} ～ {end_date.isoformat()} ====="
    )

    previous_payload = load_state()
    is_first_scan = previous_payload is None
    scan_started_iso = now_iso()
    save_runtime({
        "monitor_status": "running",
        "process_pid": os.getpid(),
        "process_started_at": load_runtime().get("process_started_at") or now_iso(),
        "last_scan_started_at": scan_started_iso,
        "last_scan_finished_at": None,
    })

    browser = None
    context = None

    try:
        (
            browser,
            context,
            page,
            html,
            current_url,
            user_agent,
            browser_charset,
            maintenance_dates,
            system_windows,
            notice_errors,
        ) = bootstrap_browser_session(playwright)

        # 同一BrowserContextのCookieを共有するHTTP API。
        # ヘッダーは通常ブラウザのUser-Agentに合わせる。
        request_context = context.request

        # メンテナンス予定日は監視対象から除外しない。
        # 未来日の予約状況は通常どおり確認し、メンテナンス当日になった時点で
        # TOSS本体へのアクセスを止めて「メンテナンス中」と判定する。
        target_dates = all_target_dates
        future_maintenance_dates = sorted(
            d for d in maintenance_dates
            if today < d <= end_date
        )
        trace(
            f"監視対象日数={len(target_dates)} / 月曜除外 / "
            f"将来のTOSS定期メンテナンス予定={len(future_maintenance_dates)}"
        )
        if future_maintenance_dates:
            trace(
                "将来のTOSS定期メンテナンス日は予約状況を監視し、"
                "当日だけTOSS利用不可として扱います: "
                + ", ".join(d.isoformat() for d in future_maintenance_dates)
            )

        results_by_date = {}
        errors_by_date = {}

        for target_date in target_dates:
            ensure_not_maintenance()
            date_str = target_date.isoformat()
            trace(f"日付取得開始: {date_str}")

            try:
                facilities, html, current_url = scan_one_date_http(
                    request_context,
                    page,
                    html,
                    current_url,
                    user_agent,
                    target_date,
                    system_windows,
                    browser_charset,
                )
                results_by_date[date_str] = facilities
                trace(f"日付取得完了: {date_str}")
            except Exception as exc:
                errors_by_date[date_str] = f"{type(exc).__name__}: {exc}"
                trace(
                    f"日付取得エラー: {date_str} | "
                    f"{type(exc).__name__}: {exc}"
                )
                # 1日でも取得失敗したら、後続のHTTP送信は停止。
                break

        complete = (
            not errors_by_date
            and len(results_by_date) == len(target_dates)
        )

        if not complete:
            result_text = format_results(
                results_by_date,
                [],
                errors_by_date,
                notice_errors=notice_errors,
                maintenance_dates=maintenance_dates,
                range_start=today,
                range_end=end_date,
            )
            save_result(result_text)
            details = []
            for date_str in sorted(errors_by_date):
                details.append(f"{date_str}: {errors_by_date[date_str]}")
            error_blob = " / ".join(details)
            page_words = ("件数", "allCount", "DOM", "フォーム", "施設", "icon", "日付", "ページ")
            error_category = (
                "TOSSページ構造エラー"
                if any(word in error_blob for word in page_words)
                else "TOSS取得エラー"
            )
            save_runtime({
                "monitor_status": "error",
                "last_scan_finished_at": now_iso(),
                "last_success_at": load_runtime().get("last_success_at"),
                "errors": {
                    "TOSS取得エラー": error_blob if error_category == "TOSS取得エラー" else None,
                    "お知らせ取得エラー": notice_errors or None,
                    "TOSSページ構造エラー": error_blob if error_category == "TOSSページ構造エラー" else None,
                    "状態ファイルエラー": None,
                },
            })
            trace("取得が不完全なため、状態ファイルは更新しません。")
            print(result_text)
            print(f"\n保存先: {RESULT_FILE}")
            print(f"状態保存先: {STATE_FILE}（今回は更新なし）")
            return []

        current_statuses = build_current_state(results_by_date)
        changes = detect_non_available_to_available(previous_payload, current_statuses)

        if is_first_scan and not INITIAL_SCAN_NOTIFY:
            trace(
                "初回監視のため、現在の○は通知対象にせず"
                "状態だけ保存します。"
            )
            changes = []

        result_text = format_results(
            results_by_date,
            changes,
            {},
            notice_errors=notice_errors,
            maintenance_dates=maintenance_dates,
            range_start=today,
            range_end=end_date,
        )
        save_result(result_text)

        # 未来のメンテナンス予定日も通常取得して状態保存する。
        # メンテナンス当日にTOSSへ入れない間は、その日の状態を上書きしない。
        keep_dates = {d.isoformat() for d in all_target_dates}
        merged_statuses = {}
        if isinstance(previous_payload, dict):
            previous_statuses = previous_payload.get("statuses", {})
            if isinstance(previous_statuses, dict):
                for key, value in previous_statuses.items():
                    date_part = str(key).split("|", 1)[0]
                    if date_part in keep_dates:
                        merged_statuses[key] = value

        for date_str in results_by_date:
            prefix = f"{date_str}|"
            for key in [k for k in merged_statuses if k.startswith(prefix)]:
                del merged_statuses[key]

        merged_statuses.update(current_statuses)
        save_state(
            merged_statuses,
            maintenance_dates=maintenance_dates,
            system_windows=system_windows,
        )

        save_runtime({
            "monitor_status": "ok",
            "last_scan_finished_at": now_iso(),
            "last_success_at": now_iso(),
            "last_status_count": len(current_statuses),
            "last_scan_has_notice_error": bool(notice_errors),
            "errors": {
                "TOSS取得エラー": None,
                "お知らせ取得エラー": notice_errors or None,
                "TOSSページ構造エラー": None,
                "状態ファイルエラー": None,
            },
            "maintenance_dates": [d.isoformat() for d in sorted(maintenance_dates)],
            "system_maintenance_windows": [
                {"start": a.isoformat(), "end": b.isoformat()}
                for a, b in system_windows
            ],
        })

        trace(
            f"HTTP監視完了: 日数={len(results_by_date)}, "
            f"ステータス数={len(current_statuses)}, "
            f"空き化（○以外→○）={len(changes)}"
        )

        print(result_text)
        print(f"\n保存先: {RESULT_FILE}")
        print(f"状態保存先: {STATE_FILE}")
        return changes

    finally:
        if browser is not None:
            browser.close()


def main():
    TRACE_FILE.write_text("", encoding="utf-8")
    process_started_at = now_iso()
    save_runtime({
        "monitor_status": "starting",
        "process_pid": os.getpid(),
        "process_started_at": process_started_at,
        "last_scan_started_at": None,
        "last_scan_finished_at": None,
    })

    try:
        with sync_playwright() as p:
            while True:
                # 01:00～07:00はTOSSへのアクセスを行わない。
                if is_maintenance_time():
                    save_maintenance_status()
                    sleep_seconds = seconds_until_maintenance_end()
                    trace(
                        "夜間停止時間（01:00～07:00）のため監視を停止します。"
                    )
                    trace(
                        f"07:00まで {sleep_seconds:.1f}秒待機します。"
                    )
                    time.sleep(sleep_seconds)
                    trace("メンテナンス終了。次の監視を開始します。")
                    continue

                cycle_started = time.monotonic()

                try:
                    scan_once(p)
                except ScheduledSystemMaintenanceError as exc:
                    save_maintenance_status(str(exc))
                    save_runtime({
                        "monitor_status": "maintenance",
                        "maintenance_reason": str(exc),
                        "maintenance_until": exc.end_time.isoformat(),
                        "errors": {
                            "TOSS取得エラー": None,
                            "お知らせ取得エラー": None,
                            "TOSSページ構造エラー": None,
                            "状態ファイルエラー": None,
                        },
                    })
                    trace(str(exc))
                    sleep_seconds = max(0.0, (exc.end_time - datetime.now(JST)).total_seconds())
                    trace(
                        f"システムメンテナンス終了まで {sleep_seconds:.1f}秒待機します。"
                    )
                    time.sleep(sleep_seconds)
                    trace("システムメンテナンス終了。次の監視を開始します。")
                    continue
                except ScheduledMaintenanceError as exc:
                    save_runtime({
                        "monitor_status": "maintenance",
                        "maintenance_reason": str(exc),
                        "maintenance_until": (datetime.now(JST).date() + timedelta(days=1)).isoformat() + "T00:00:00+09:00",
                        "errors": {
                            "TOSS取得エラー": None,
                            "お知らせ取得エラー": None,
                            "TOSSページ構造エラー": None,
                            "状態ファイルエラー": None,
                        },
                    })
                    # お知らせで当日が終日メンテナンス対象と確認できた場合は、
                    # 当日の無駄な再アクセスを避け、翌日まで停止する。
                    save_maintenance_status(str(exc))
                    trace(str(exc))
                    sleep_seconds = seconds_until_next_day()
                    trace(
                        f"終日メンテナンス対象日のため、翌日00:00まで {sleep_seconds:.1f}秒待機します。"
                    )
                    time.sleep(sleep_seconds)
                    trace("日付切替。次の監視を開始します。")
                    continue
                except MaintenanceWindowError as exc:
                    save_runtime({
                        "monitor_status": "maintenance",
                        "maintenance_reason": str(exc),
                        "maintenance_until": (datetime.now(JST).date().isoformat() + "T07:00:00+09:00"),
                        "errors": {
                            "TOSS取得エラー": None,
                            "お知らせ取得エラー": None,
                            "TOSSページ構造エラー": None,
                            "状態ファイルエラー": None,
                        },
                    })
                    # 監視途中で03:00を迎えた場合もエラー扱いにせず、
                    # LINEから確認したときにメンテナンス表示になるようにする。
                    save_maintenance_status()
                    trace(str(exc))
                    sleep_seconds = seconds_until_maintenance_end()
                    trace(
                        f"夜間停止終了まで {sleep_seconds:.1f}秒待機します。"
                    )
                    time.sleep(sleep_seconds)
                    trace("メンテナンス終了。次の監視を開始します。")
                    continue
                except StateFileError as exc:
                    error_text = (
                        "===== TOSS監視テスト エラー =====\n"
                        f"状態ファイルエラー: {type(exc).__name__}: {exc}\n"
                    )
                    save_runtime({
                        "monitor_status": "error",
                        "errors": {
                            "TOSS取得エラー": None,
                            "お知らせ取得エラー": None,
                            "TOSSページ構造エラー": None,
                            "状態ファイルエラー": f"{type(exc).__name__}: {exc}",
                        },
                    })
                    save_result(error_text)
                    trace(error_text.replace("\n", " | "))
                    print(error_text)
                except Exception as exc:
                    error_text = (
                        "===== TOSS監視テスト エラー =====\n"
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    message = str(exc)
                    page_words = ("件数", "allCount", "DOM", "フォーム", "施設", "icon", "日付", "ページ")
                    category = "TOSSページ構造エラー" if any(word in message for word in page_words) else "TOSS取得エラー"
                    save_runtime({
                        "monitor_status": "error",
                        "errors": {
                            "TOSS取得エラー": message if category == "TOSS取得エラー" else None,
                            "お知らせ取得エラー": None,
                            "TOSSページ構造エラー": message if category == "TOSSページ構造エラー" else None,
                            "状態ファイルエラー": None,
                        },
                    })
                    save_result(error_text)
                    trace(error_text.replace("\n", " | "))
                    print(error_text)

                elapsed = time.monotonic() - cycle_started
                sleep_seconds = max(0, CHECK_INTERVAL_SECONDS - elapsed)

                if sleep_seconds > 0:
                    trace(
                        f"今回の処理時間={elapsed:.1f}秒。"
                        f"要求開始から5分後まで {sleep_seconds:.1f}秒待機します。"
                    )
                    time.sleep(sleep_seconds)
                else:
                    trace(
                        f"今回の処理時間={elapsed:.1f}秒で5分を超えたため、"
                        "待機せず次サイクルを開始します。"
                    )

    except KeyboardInterrupt:
        trace("Ctrl+Cで終了しました。")


if __name__ == "__main__":
    main()