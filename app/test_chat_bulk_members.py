from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from types import SimpleNamespace
from uuid import uuid4

from app.chat_api import GroupMemberIn, _can_see, join_group, router


def test_group_member_request_accepts_multiple_employee_codes():
    request = GroupMemberIn(employeeCodes=["EMP-001", "EMP-002"])
    assert request.employeeCodes == ["EMP-001", "EMP-002"]
    assert request.employeeCode is None


def test_group_member_request_keeps_legacy_single_employee_code():
    request = GroupMemberIn(employeeCode="EMP-001")
    assert request.employeeCode == "EMP-001"
    assert request.employeeCodes == []


def test_group_member_request_limits_bulk_add_to_100_employees():
    with pytest.raises(ValidationError):
        GroupMemberIn(employeeCodes=[f"EMP-{index:03d}" for index in range(101)])


def test_employee_sees_only_groups_the_admin_added_them_to():
    employee_id = str(uuid4())
    employee = SimpleNamespace(id=employee_id, role="employee")
    unassigned_group = {"type": "group", "memberIds": []}
    assigned_group = {"type": "group", "memberIds": [employee_id]}

    assert not _can_see(unassigned_group, employee)
    assert _can_see(assigned_group, employee)


def test_group_visibility_restriction_does_not_hide_direct_conversations():
    employee_id = str(uuid4())
    employee = SimpleNamespace(id=employee_id, role="employee")

    assert _can_see({"type": "direct", "directUserId": employee_id}, employee)
    assert not _can_see({"type": "direct", "directUserId": str(uuid4())}, employee)


def test_employee_cannot_join_group_without_admin_assignment():
    employee = SimpleNamespace(id=str(uuid4()), role="employee")

    with pytest.raises(HTTPException) as error:
        join_group(str(uuid4()), user=employee, db=None)

    assert error.value.status_code == 403


def test_admin_group_member_list_and_removal_routes_are_registered():
    route_methods = {
        (route.path, method)
        for route in router.routes
        for method in (route.methods or set())
    }

    assert ("/chats/{chat_id}/members", "GET") in route_methods
    assert ("/chats/{chat_id}/members/{employee_code}", "DELETE") in route_methods


# from fastapi import HTTPException
# from pydantic import ValidationError
# import pytest
# from types import SimpleNamespace
# from uuid import uuid4

# from app.chat_api import GroupMemberIn, _can_see, join_group


# def test_group_member_request_accepts_multiple_employee_codes():
#     request = GroupMemberIn(employeeCodes=["EMP-001", "EMP-002"])
#     assert request.employeeCodes == ["EMP-001", "EMP-002"]
#     assert request.employeeCode is None


# def test_group_member_request_keeps_legacy_single_employee_code():
#     request = GroupMemberIn(employeeCode="EMP-001")
#     assert request.employeeCode == "EMP-001"
#     assert request.employeeCodes == []


# def test_group_member_request_limits_bulk_add_to_100_employees():
#     with pytest.raises(ValidationError):
#         GroupMemberIn(employeeCodes=[f"EMP-{index:03d}" for index in range(101)])


# def test_employee_sees_only_groups_the_admin_added_them_to():
#     employee_id = str(uuid4())
#     employee = SimpleNamespace(id=employee_id, role="employee")
#     unassigned_group = {"type": "group", "memberIds": []}
#     assigned_group = {"type": "group", "memberIds": [employee_id]}

#     assert not _can_see(unassigned_group, employee)
#     assert _can_see(assigned_group, employee)


# def test_group_visibility_restriction_does_not_hide_direct_conversations():
#     employee_id = str(uuid4())
#     employee = SimpleNamespace(id=employee_id, role="employee")

#     assert _can_see({"type": "direct", "directUserId": employee_id}, employee)
#     assert not _can_see({"type": "direct", "directUserId": str(uuid4())}, employee)


# def test_employee_cannot_join_group_without_admin_assignment():
#     employee = SimpleNamespace(id=str(uuid4()), role="employee")

#     with pytest.raises(HTTPException) as error:
#         join_group(str(uuid4()), user=employee, db=None)

#     assert error.value.status_code == 403
