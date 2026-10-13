from app.main import app


def test_contract_has_required_frontend_paths():
    paths = set(app.openapi()["paths"])
    required = {
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/employees",
        "/api/v1/workplaces",
        "/api/v1/attendance/clock-in",
        "/api/v1/attendance/clock-out",
        "/api/v1/me/balance",
        "/api/v1/salary/summary",
        "/api/v1/settings",
        "/api/v1/leaves",
        "/api/v1/equipment",
        "/api/v1/loans",
        "/api/v1/chats",
        "/api/v1/sites",
        "/api/v1/clients",
        "/api/v1/documents",
        "/health",
        "/ready",
    }
    assert required <= paths, f"Routes absentes : {sorted(required - paths)}"
    assert any(getattr(route, "path", None) == "/api/v1/ws" for route in app.routes)


def test_openapi_has_expected_operation_coverage():
    schema = app.openapi()
    operation_count = sum(len(methods) for methods in schema["paths"].values())
    assert operation_count >= 100
    assert schema["info"]["title"] == "Pointage Fortico API"
