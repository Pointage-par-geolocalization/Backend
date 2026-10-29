from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
import runpy

import jwt
from sqlalchemy.orm import configure_mappers

from app import store  # noqa: F401 - registers DomainRecord
from app.core import issue_access, settings
from app.models import Base, Role


def test_every_business_table_has_a_non_nullable_tenant_owner():
    configure_mappers()
    assert "auth_login_attempts" in Base.metadata.tables
    for name, table in Base.metadata.tables.items():
        if name == "auth_login_attempts":
            continue  # rate-limit counters are security telemetry, not tenant business data
        assert "tenant_id" in table.c, f"{name} is missing tenant_id"
        assert table.c.tenant_id.nullable is False, f"{name}.tenant_id must be required"
        assert any(fk.target_fullname == "users.id" for fk in table.c.tenant_id.foreign_keys)


def test_tenant_local_singletons_and_document_numbers_have_composite_keys():
    for name, expected in {
        "settings": ("tenant_id", "id"),
        "document_counters": ("tenant_id", "document_type", "year"),
        "calendar_settings": ("tenant_id", "id"),
        "business_logo": ("tenant_id", "id"),
        "domain_records": ("tenant_id", "id"),
    }.items():
        assert tuple(col.name for col in Base.metadata.tables[name].primary_key.columns) == expected
    documents = Base.metadata.tables["documents"]
    assert any(
        {column.name for column in constraint.columns} == {"tenant_id", "number"}
        for constraint in documents.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )


def test_database_migration_enables_and_forces_rls_on_every_tenant_table():
    migration = Path(__file__).parents[1] / "migrations/versions/9c4f2d7a1e60_admin_tenant_isolation.py"
    source = migration.read_text()
    tenant_tables = set(runpy.run_path(str(migration))["TENANT_TABLES"])
    expected_tables = set(Base.metadata.tables) - {"auth_login_attempts"}
    assert expected_tables <= tenant_tables
    assert 'ENABLE ROW LEVEL SECURITY' in source
    assert 'FORCE ROW LEVEL SECURITY' in source
    assert "WITH CHECK (tenant_id =" in source
    assert "tenant_id = CASE WHEN role::text = 'admin' THEN id" in source


def test_access_token_is_bound_to_the_users_tenant():
    tenant_id = uuid4()
    user_id = uuid4()

    user = SimpleNamespace(id=user_id, role=Role.admin, employee_code=None,
                           token_version=3, tenant_id=tenant_id)

    token = issue_access(user)
    claims = jwt.decode(token, settings.jwt_access_secret, algorithms=["HS256"])
    assert claims["sub"] == str(user_id)
    assert claims["tenantId"] == str(tenant_id)
    assert claims["ver"] == 3
