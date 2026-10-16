"""Ad-hoc external-agent history is readable without fabricating a workflow."""

import asyncio
import json

import pytest

from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.services import media_service as media_module
from apps.admin_console.services.media_service import MediaService


@pytest.fixture
def report_store(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(media_module, "TRACES_PATH", tmp_path)
    monkeypatch.setattr(media_module, "session_repo", SessionRepository(db_path))
    return db_path, tmp_path


def insert_session(db_path, status="completed", *, mode="external_agent"):
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, status, device_info) VALUES (?, ?, ?, ?)",
            (
                "session-1",
                "验证插屏和激励广告",
                status,
                json.dumps(
                    {
                        "execution_mode": mode,
                        "profile": "codex",
                        "device_id": "test-device",
                    }
                ),
            ),
        )
        conn.commit()


def insert_trace(db_path, name, payload, timestamp=1, image_name=None):
    trace_id = f"{name}-{timestamp}"
    with db_session(db_path) as conn:
        if image_name:
            conn.execute(
                "INSERT INTO steps (step_id, session_id, pre_image_name) VALUES (?, ?, ?)",
                (trace_id, "session-1", image_name),
            )
        conn.execute(
            "INSERT INTO traces (trace_id, session_id, step_id, type, name, timestamp, status, payload) "
            "VALUES (?, ?, ?, 'tool', ?, ?, 'failed', ?)",
            (trace_id, "session-1", trace_id, name, timestamp, json.dumps(payload)),
        )
        conn.commit()


def test_history_report_preserves_summary_and_inconclusive_evidence(report_store):
    db_path, root = report_store
    insert_session(db_path)
    for index in range(13):
        insert_trace(
            db_path,
            "verify",
            {
                "args": {
                    "assertion": f"检查 {index + 1}",
                    "status": "passed" if index < 12 else "inconclusive",
                    "evidence": "奖励回调一次"
                    if index < 12
                    else "素材没有提前关闭入口，无法验证。",
                }
            },
            index,
            image_name="screenshot-1" if index == 12 else None,
        )
    summary = "12项通过，1项未验证：提前关闭缺少素材入口。"
    insert_trace(db_path, "codex_summary", {"summary": summary}, 20)

    notes = MediaService.get_session_notes_content("session-1")

    assert set(notes) == {"session_context.md", "test_report.md"}
    assert "验证插屏和激励广告" in notes["session_context.md"]
    assert "no advance plan is inferred" in notes["session_context.md"]
    report = notes["test_report.md"]
    assert summary in report
    assert "12 passed · 0 failed · 1 inconclusive" in report
    assert "Outcome: **inconclusive**" in report
    assert "素材没有提前关闭入口，无法验证。" in report
    assert "Screenshot evidence is saved" in report
    assert not (root / "session-1").exists()


def test_failed_history_can_recover_summary_from_status_json(report_store):
    db_path, root = report_store
    insert_session(db_path, status="failed")
    directory = root / "session-1"
    directory.mkdir()
    (directory / "status.json").write_text(
        json.dumps({"error": "两次 LOAD 均为 No fill，展示未验证。"})
    )

    report = MediaService.get_session_notes_content("session-1")["test_report.md"]

    assert "Run status: **failed**" in report
    assert "两次 LOAD 均为 No fill，展示未验证。" in report
    assert "test success is unverified" in report
    assert not (directory / "notes").exists()


def test_cancelled_summary_comes_from_trace_without_status_result(report_store):
    db_path, root = report_store
    insert_session(db_path, status="cancelled")
    insert_trace(db_path, "codex_summary", {"summary": "用户结束测试，未完成验证。"})

    report = MediaService.get_session_notes_content("session-1")["test_report.md"]

    assert "Run status: **cancelled**" in report
    assert "用户结束测试，未完成验证。" in report
    assert not (root / "session-1").exists()


def test_running_session_has_progress_and_no_final_verdict(report_store):
    db_path, _ = report_store
    insert_session(db_path, status="running")

    report = MediaService.get_session_notes_content("session-1")["test_report.md"]

    assert "## Current progress" in report
    assert "## Final conclusion" not in report
    assert "session is still open" in report
    assert "0 passed · 0 failed · 0 inconclusive" in report
    assert "test success is unverified" in report


def test_existing_workflow_notes_are_owned_by_the_workflow(report_store, monkeypatch):
    _, root = report_store
    directory = root / "session-1"
    notes_dir = directory / "notes"
    notes_dir.mkdir(parents=True)
    (directory / "test_workflow.json").write_text("{}")
    (notes_dir / "test_report.md").write_text("Workflow report")
    (notes_dir / "test_plan.md").write_text("Actual advance plan")

    def unexpected_read(_session_id):
        pytest.fail("Workflow reports must not use the ad-hoc fallback")

    monkeypatch.setattr(media_module.session_repo, "get_external_execution_report", unexpected_read)

    assert MediaService.get_session_notes_content("session-1") == {
        "test_report.md": "Workflow report",
        "test_plan.md": "Actual advance plan",
    }


def test_fallback_preserves_existing_notes_and_report(report_store):
    db_path, root = report_store
    insert_session(db_path)
    notes_dir = root / "session-1" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "test_report.md").write_text("User's report")
    (notes_dir / "session_context.md").write_text("User's context")
    (notes_dir / "task_plan.md").write_text("Actual plan")

    assert MediaService.get_session_notes_content("session-1") == {
        "test_report.md": "User's report",
        "session_context.md": "User's context",
        "task_plan.md": "Actual plan",
    }


@pytest.mark.parametrize("mode", ["autonomous", None])
def test_non_executor_sessions_and_unknown_ids_get_no_fake_report(report_store, mode):
    db_path, _ = report_store
    insert_session(db_path, mode=mode)

    assert MediaService.get_session_notes_content("session-1") == {}
    assert MediaService.get_session_notes_content("missing") == {}


def test_malformed_records_are_skipped_and_latest_summary_wins(report_store):
    db_path, _ = report_store
    insert_session(db_path)
    insert_trace(db_path, "codex_summary", {"summary": "earlier"}, 1)
    insert_trace(db_path, "codex_summary", {"summary": "final"}, 2)
    insert_trace(db_path, "verify", {"args": {"status": "passed"}}, 3)
    insert_trace(db_path, "verify", [], 4)
    insert_trace(
        db_path, "verify", {"args": {"status": "invalid", "assertion": "a", "evidence": "b"}}, 5
    )
    insert_trace(db_path, "verify", {"args": {"status": [], "assertion": "a", "evidence": "b"}}, 6)

    report = MediaService.get_session_notes_content("session-1")["test_report.md"]

    assert "\nfinal\n" in report
    assert "earlier" not in report
    assert "0 passed · 0 failed · 0 inconclusive" in report


def test_notes_endpoint_exposes_executor_history(report_store, monkeypatch):
    from apps.admin_console.routers import media

    db_path, _ = report_store
    insert_session(db_path)
    monkeypatch.setattr(media, "media_service", MediaService())

    response = asyncio.run(media.get_all_notes("session-1"))

    assert "test_report.md" in response["notes"]
    assert "session_context.md" in response["notes"]
