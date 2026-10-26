from app.main import app
from app.models import Employee, Payment, Timesheet
from datetime import date, datetime, time
from types import SimpleNamespace


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


def test_employee_number_and_new_admin_contracts_are_exposed():
    from app.models import Employee
    from app.main import app
    assert "employee_number" in Employee.__table__.c
    paths = app.openapi()["paths"]
    assert "/api/v1/me/timesheets/prefill/{week_start}" in paths
    assert "/api/v1/admin/timesheets/{sheet_id}/unlock" in paths
    assert "/api/v1/payments/{payment_id}" in paths
    assert "patch" in paths["/api/v1/payments/{payment_id}"]


def test_timesheet_prefill_returns_seven_aggregated_days_for_journalier_employee():
    from app.api import TZ, prefill_timesheet

    monday = date(2026, 1, 5)
    employee_row = SimpleNamespace(contract_type="JOURNALIER")
    rows = [
        SimpleNamespace(id="a1", entry_time=datetime.combine(monday, time(8), tzinfo=TZ), worked_minutes=240),
        SimpleNamespace(id="a2", entry_time=datetime.combine(monday, time(13), tzinfo=TZ), worked_minutes=180),
        SimpleNamespace(id="a3", entry_time=datetime.combine(date(2026, 1, 6), time(8), tzinfo=TZ), worked_minutes=60),
    ]

    class ScalarRows:
        def all(self):
            return rows

    class FakeDB:
        def get(self, model, key):
            return employee_row if model is Employee else None
        def scalars(self, statement):
            return ScalarRows()
        def scalar(self, statement):
            return None

    result = prefill_timesheet(monday, SimpleNamespace(employee_code="EMP-01"), FakeDB())
    assert len(result["entries"]) == 7
    assert result["entries"][0] == {"hours": 7.0, "note": ""}
    assert result["entries"][1]["hours"] == 1.0
    assert len(result["sources"][0]["attendanceIds"]) == 2

# from app.main import app
# from app.models import Employee, Payment, Timesheet


# def test_timesheet_http_contract_is_registered():
#     paths = app.openapi()["paths"]
#     assert "/api/v1/me/timesheets" in paths
#     assert "/api/v1/me/timesheets/{week_start}" in paths
#     assert "/api/v1/me/timesheets/{week_start}/submit" in paths
#     assert "/api/v1/admin/timesheets" in paths
#     assert "/api/v1/admin/timesheets/{sheet_id}/approve" in paths
#     assert "/api/v1/admin/timesheets/{sheet_id}/return" in paths


# def test_contract_and_payment_audit_fields_are_in_relational_models():
#     assert "contract_type" in Employee.__table__.c
#     assert "monthly_salary" in Employee.__table__.c
#     assert {"employee_code", "week_start", "entries", "status", "approved_by"}.issubset(Timesheet.__table__.c.keys())
#     assert {"deleted_at", "deleted_by", "deletion_reason"}.issubset(Payment.__table__.c.keys())


# def test_individual_message_delete_and_bulk_clear_are_not_exposed():
#     paths = app.openapi()["paths"]
#     assert "/api/v1/chats/{chat_id}/messages/{message_id}" not in paths
#     assert "/api/v1/chats/{chat_id}/clear" not in paths
#     assert "/api/v1/chats/{chat_id}/members" in paths
