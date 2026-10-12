import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import toss_monitor_test_parallel_verified as monitor
from playwright.sync_api import sync_playwright


API_BASE = "https://toss-monitor-api.nbakaikai.workers.dev"
API_TOKEN_ENV = "TOSS_API_TOKEN"


def api_request(method, path, body=None):
    token = os.environ.get(API_TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{API_TOKEN_ENV} is not configured")

    headers = {
        "X-TOSS-API-Token": token,
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(API_BASE + path, data=data, headers=headers, method=method)

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        # 認証情報そのものは絶対にログへ出さない。
        if len(detail) > 1000:
            detail = detail[:1000] + "...(truncated)"
        raise RuntimeError(
            f"Cloudflare API HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"Cloudflare API connection failed: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Cloudflare API returned invalid JSON") from exc


def load_remote_state():
    response = api_request("GET", "/api/state")
    if response.get("exists") is False:
        return None

    state = response.get("state")
    if not isinstance(state, dict):
        raise RuntimeError("Cloudflare KV state is missing or invalid")

    statuses = state.get("statuses")
    if not isinstance(statuses, dict):
        raise RuntimeError("Cloudflare KV state has no valid statuses object")

    return state


def state_contents_equal(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return (
        a.get("statuses") == b.get("statuses")
        and a.get("maintenance_dates") == b.get("maintenance_dates")
        and a.get("system_maintenance_windows") == b.get("system_maintenance_windows")
    )


def is_final_pre_night_success(runtime):
    """夜間停止開始前15分以内に成功取得済みなら、予定停止に切り替える。"""
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    if now_jst.hour != 0:
        return False

    pause_start = now_jst.replace(hour=1, minute=0, second=0, microsecond=0)
    raw_success_at = runtime.get("last_success_at") if isinstance(runtime, dict) else None
    if not raw_success_at:
        return False

    try:
        success_at = datetime.fromisoformat(
            str(raw_success_at).replace("Z", "+00:00").replace(" ", "T")
        )
        if success_at.tzinfo is None:
            success_at = success_at.replace(tzinfo=jst)
        else:
            success_at = success_at.astimezone(jst)
    except (TypeError, ValueError):
        return False

    return pause_start - timedelta(minutes=15) <= success_at < pause_start


def is_first_post_night_scan(previous_state):
    if not isinstance(previous_state, dict):
        return True

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    if now_jst.hour < 7:
        return False

    saved_at = previous_state.get("saved_at")
    if not saved_at:
        return True

    try:
        saved_epoch = datetime.fromisoformat(
            str(saved_at).replace("Z", "+00:00").replace(" ", "T")
        ).replace(tzinfo=timezone(timedelta(hours=9))).timestamp()
    except (TypeError, ValueError):
        return True

    today_start = now_jst.replace(hour=7, minute=0, second=0, microsecond=0)
    return saved_epoch < today_start.timestamp()


def write_local_state(state):
    monitor.STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )


def read_local_state():
    if not monitor.STATE_FILE.exists():
        raise RuntimeError("TOSS監視後の状態ファイルが作成されませんでした")

    try:
        state = json.loads(monitor.STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("TOSS監視後の状態ファイルが不正なJSONです") from exc

    if not isinstance(state, dict):
        raise RuntimeError("TOSS監視後の状態がJSONオブジェクトではありません")

    if not isinstance(state.get("statuses"), dict):
        raise RuntimeError("TOSS監視後のstatusesが不正です")

    return state


def save_state_to_cloudflare(state):
    response = api_request("PUT", "/api/state", state)
    if response.get("ok") is not True:
        raise RuntimeError(
            "Cloudflare KVへの状態保存に失敗しました: "
            + json.dumps(response, ensure_ascii=False)
        )

    print(
        "Cloudflare KV保存成功: "
        f"bytes={response.get("bytes")}, "
        f"backup_saved={response.get("backup_saved")}"
    )


def save_runtime_to_cloudflare(runtime):
    if not isinstance(runtime, dict):
        raise RuntimeError("TOSS runtimeがJSONオブジェクトではありません")

    response = api_request("PUT", "/api/runtime", runtime)
    if response.get("ok") is not True:
        raise RuntimeError(
            "Cloudflare KVへのruntime保存に失敗しました: "
            + json.dumps(response, ensure_ascii=False)
        )

    print(
        "Cloudflare KV runtime保存成功: "
        f"bytes={response.get("bytes")}"
    )


def main():
    monitor.TRACE_FILE.write_text("", encoding="utf-8")
    process_started_at = monitor.now_iso()

    monitor.save_runtime({
        "monitor_status": "starting",
        "process_pid": os.getpid(),
        "process_started_at": process_started_at,
        "last_scan_started_at": None,
        "last_scan_finished_at": None,
    })

    config = api_request("GET", "/api/config")
    monitor_enabled = bool(config.get("monitor_enabled", False))

    # TOSS状態の定期取得は、LINEの監視モードON/OFFとは独立して常時実行する。
    # monitor_enabled はLINE側の「○以外→○」通知を有効にするための設定であり、
    # 最新のTOSS状態をCloudflare KVへ更新する処理を停止させてはいけない。
    print(
        "Cloudflare監視設定: "
        f"monitor_enabled={monitor_enabled}（TOSS状態取得は常時実行）"
    )


    previous_state = load_remote_state()

    if previous_state is None:
        try:
            monitor.STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        print("KVに前回状態がないため、初回取得として実行します。")
    else:
        write_local_state(previous_state)
        print(f"KVから前回状態を復元しました: statuses={len(previous_state['statuses'])}")

    night_pause_interrupted = False
    try:
        with sync_playwright() as playwright:
            monitor.scan_once(playwright)
    except monitor.MaintenanceWindowError as exc:
        # 01:00をまたぐなど、予定された夜間停止に入った場合は正常停止として記録。
        night_pause_interrupted = True
        now_jst = datetime.now(timezone(timedelta(hours=9)))
        until_jst = now_jst.replace(
            hour=7, minute=0, second=0, microsecond=0
        )
        monitor.save_runtime({
            "monitor_status": "maintenance",
            "maintenance_reason": str(exc),
            "maintenance_until": until_jst.isoformat(),
            "last_scan_finished_at": monitor.now_iso(),
            "errors": {
                "TOSS取得エラー": None,
                "お知らせ取得エラー": None,
                "TOSSページ構造エラー": None,
                "状態ファイルエラー": None,
            },
        })
        print(f"予定された夜間停止のため監視を正常終了します: {exc}")
    finally:
        runtime = monitor.load_runtime()
        if runtime:
            save_runtime_to_cloudflare(runtime)

    if night_pause_interrupted:
        print("前回の正常なTOSS状態を保持し、状態の更新は行いません。")
        return

    runtime = monitor.load_runtime()
    if runtime.get("monitor_status") != "ok":
        raise RuntimeError(
            "TOSS監視が正常終了していないため、KV状態は更新しません。"
        )

    current_state = read_local_state()
    first_post_night_scan = is_first_post_night_scan(previous_state)
    if previous_state is None or first_post_night_scan or not state_contents_equal(previous_state, current_state):
        save_state_to_cloudflare(current_state)
    else:
        print("TOSS state保存スキップ: 前回と同一内容です。")

    runtime = monitor.load_runtime()
    if is_final_pre_night_success(runtime):
        now_jst = datetime.now(timezone(timedelta(hours=9)))
        until_jst = now_jst.replace(hour=7, minute=0, second=0, microsecond=0)
        monitor.save_runtime({
            "monitor_status": "maintenance",
            "maintenance_reason": (
                "夜間停止前15分以内の正常取得を完了したため、"
                "予定どおり監視を停止します。"
            ),
            "maintenance_until": until_jst.isoformat(),
            "errors": {
                "TOSS取得エラー": None,
                "お知らせ取得エラー": None,
                "TOSSページ構造エラー": None,
                "状態ファイルエラー": None,
            },
        })
        save_runtime_to_cloudflare(monitor.load_runtime())
        print(
            "夜間停止前15分以内の正常取得を確認しました。"
            "今回の取得を最後にし、07:00まで予定停止します。"
        )

    print(
        "TOSS取得 完了: "
        f"statuses={len(current_state['statuses'])}"
    )


if __name__ == "__main__":
    main()
