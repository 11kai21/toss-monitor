import json
import os
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
    }

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(API_BASE + path, data=data, headers=headers, method=method)

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Cloudflare API request failed: {exc}") from exc

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


def main():
    force_scan = os.environ.get("TOSS_FORCE_SCAN") == "1"

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

    print(f"Cloudflare監視設定: monitor_enabled={monitor_enabled}")

    if not force_scan and not monitor_enabled:
        print("監視OFFのため、今回はTOSSへアクセスしません。")
        return

    previous_state = load_remote_state()

    if previous_state is None:
        try:
            monitor.STATE_FILE.unlink()
        except FileNotFoundError:
            pass
        print("KVに前回状態がないため、初回取得として実行します。")
    else:
        write_local_state(previous_state)
        print(f"KVから前回状態を復元しました: statuses={len(previous_state["statuses"])}")

    with sync_playwright() as playwright:
        monitor.scan_once(playwright)

    runtime = monitor.load_runtime()
    if runtime.get("monitor_status") != "ok":
        raise RuntimeError(
            "TOSS監視が正常終了していないため、KV状態は更新しません。"
        )

    current_state = read_local_state()
    save_state_to_cloudflare(current_state)

    print(
        "TOSS取得＋KV保存 完了: "
        f"statuses={len(current_state["statuses"])}"
    )


if __name__ == "__main__":
    main()
