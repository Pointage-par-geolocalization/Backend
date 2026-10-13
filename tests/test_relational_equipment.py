from app.models import Base, Equipment, EquipmentLoan, EquipmentStatus, LoanStatus


def test_equipment_and_loans_are_relational_tables():
    assert Equipment.__tablename__ == "equipment"
    assert EquipmentLoan.__tablename__ == "equipment_loans"
    assert set(Base.metadata.tables["equipment"].columns.keys()) >= {
        "id", "name", "category", "serial_number", "description", "status", "created_at"
    }
    assert set(Base.metadata.tables["equipment_loans"].columns.keys()) >= {
        "id", "equipment_id", "equipment_name", "employee_code", "employee_name",
        "status", "expected_return_date", "requested_at", "loaned_at", "returned_at"
    }


def test_status_enums_and_partial_integrity_indexes():
    assert EquipmentStatus.EMPRUNTE.value == "EMPRUNTE"
    assert LoanStatus.RETOUR_DEMANDE.value == "RETOUR_DEMANDE"
    index_names = {index.name for index in EquipmentLoan.__table__.indexes}
    assert "uq_equipment_loan_one_active" in index_names
    assert "uq_equipment_loan_one_pending_per_employee" in index_names
