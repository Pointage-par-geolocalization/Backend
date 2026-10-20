from app.main import app
from app.models import Employee, Payment, Timesheet


def test_timesheet_http_contract_is_registered():
    paths = app.openapi()["paths"]
    assert "/api/v1/me/timesheets" in paths
    assert "/api/v1/me/timesheets/{week_start}" in paths
    assert "/api/v1/me/timesheets/{week_start}/submit" in paths
    assert "/api/v1/admin/timesheets" in paths
    assert "/api/v1/admin/timesheets/{sheet_id}/approve" in paths
    assert "/api/v1/admin/timesheets/{sheet_id}/return" in paths


def test_contract_and_payment_audit_fields_are_in_relational_models():
    assert "contract_type" in Employee.__table__.c
    assert "monthly_salary" in Employee.__table__.c
    assert {"employee_code", "week_start", "entries", "status", "approved_by"}.issubset(Timesheet.__table__.c.keys())
    assert {"deleted_at", "deleted_by", "deletion_reason"}.issubset(Payment.__table__.c.keys())


def test_individual_message_delete_and_bulk_clear_are_not_exposed():
    paths = app.openapi()["paths"]
    assert "/api/v1/chats/{chat_id}/messages/{message_id}" not in paths
    assert "/api/v1/chats/{chat_id}/clear" not in paths
    assert "/api/v1/chats/{chat_id}/members" in paths
