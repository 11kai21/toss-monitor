import os
from datetime import datetime, timedelta

import toss_monitor_test_parallel_verified as monitor
from playwright.sync_api import sync_playwright


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

    if monitor.is_maintenance_time():
        monitor.save_maintenance_status()
        monitor.save_runtime({
            "monitor_status": "maintenance",
            "maintenance_reason": "TOSSメンテナンス時間（03:00～04:00）のため監視を中断します。",
            "maintenance_until": (
                datetime.now(monitor.JST).date().isoformat()
                + "T04:00:00+09:00"
            ),
            "errors": {
                "TOSS取得エラー": None,
                "お知らせ取得エラー": None,
                "TOSSページ構造エラー": None,
                "状態ファイルエラー": None,
            },
        })
        monitor.trace("TOSSメンテナンス時間のため、今回の取得をスキップしました。")
        print("TOSSメンテナンス時間（03:00～04:00）のため、今回は取得をスキップしました。")
        return

    with sync_playwright() as playwright:
        try:
            changes = monitor.scan_once(playwright)
            monitor.trace(f"1回取得完了。空き化通知候補={len(changes)}")
            print("GitHub Actions用TOSS監視：1回の取得が完了しました。")

        except monitor.ScheduledSystemMaintenanceError as exc:
            monitor.save_maintenance_status(str(exc))
            monitor.save_runtime({
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
            monitor.trace(str(exc))
            print(str(exc))

        except monitor.ScheduledMaintenanceError as exc:
            monitor.save_maintenance_status(str(exc))
            monitor.save_runtime({
                "monitor_status": "maintenance",
                "maintenance_reason": str(exc),
                "maintenance_until": (
                    datetime.now(monitor.JST).date() + timedelta(days=1)
                ).isoformat() + "T00:00:00+09:00",
                "errors": {
                    "TOSS取得エラー": None,
                    "お知らせ取得エラー": None,
                    "TOSSページ構造エラー": None,
                    "状態ファイルエラー": None,
                },
            })
            monitor.trace(str(exc))
            print(str(exc))

        except monitor.MaintenanceWindowError as exc:
            monitor.save_maintenance_status(str(exc))
            monitor.save_runtime({
                "monitor_status": "maintenance",
                "maintenance_reason": str(exc),
                "maintenance_until": (
                    datetime.now(monitor.JST).date().isoformat()
                    + "T04:00:00+09:00"
                ),
                "errors": {
                    "TOSS取得エラー": None,
                    "お知らせ取得エラー": None,
                    "TOSSページ構造エラー": None,
                    "状態ファイルエラー": None,
                },
            })
            monitor.trace(str(exc))
            print(str(exc))

        except monitor.StateFileError as exc:
            error_text = (
                "===== TOSS監視エラー =====\n"
                f"状態ファイルエラー: {type(exc).__name__}: {exc}\n"
            )
            monitor.save_runtime({
                "monitor_status": "error",
                "errors": {
                    "TOSS取得エラー": None,
                    "お知らせ取得エラー": None,
                    "TOSSページ構造エラー": None,
                    "状態ファイルエラー": f"{type(exc).__name__}: {exc}",
                },
            })
            monitor.save_result(error_text)
            monitor.trace(error_text.replace("\n", " | "))
            print(error_text)
            raise

        except Exception as exc:
            error_text = (
                "===== TOSS監視エラー =====\n"
                f"{type(exc).__name__}: {exc}\n"
            )
            message = str(exc)
            page_words = (
                "件数", "allCount", "DOM", "フォーム",
                "施設", "icon", "日付", "ページ"
            )
            category = (
                "TOSSページ構造エラー"
                if any(word in message for word in page_words)
                else "TOSS取得エラー"
            )
            monitor.save_runtime({
                "monitor_status": "error",
                "errors": {
                    "TOSS取得エラー": message if category == "TOSS取得エラー" else None,
                    "お知らせ取得エラー": None,
                    "TOSSページ構造エラー": message if category == "TOSSページ構造エラー" else None,
                    "状態ファイルエラー": None,
                },
            })
            monitor.save_result(error_text)
            monitor.trace(error_text.replace("\n", " | "))
            print(error_text)
            raise


if __name__ == "__main__":
    main()
