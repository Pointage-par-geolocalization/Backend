from __future__ import annotations
from datetime import datetime, date, timezone
from uuid import uuid4
from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, CheckConstraint, Index, func, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
import enum

class Base(DeclarativeBase): pass
class Role(str, enum.Enum): admin="admin"; employee="employee"
class AttendanceStatus(str, enum.Enum): EN_COURS="EN_COURS"; TERMINEE="TERMINEE"
class PunchSource(str, enum.Enum): EMPLOYEE="EMPLOYEE"; ADMIN="ADMIN"
class EquipmentStatus(str, enum.Enum): DISPONIBLE="DISPONIBLE"; EMPRUNTE="EMPRUNTE"; HORS_SERVICE="HORS_SERVICE"
class LoanStatus(str, enum.Enum): DEMANDE="DEMANDE"; REFUSEE="REFUSEE"; ANNULEE="ANNULEE"; EN_COURS="EN_COURS"; RETOUR_DEMANDE="RETOUR_DEMANDE"; RETOURNE="RETOURNE"
class LeaveStatus(str, enum.Enum): EN_ATTENTE="EN_ATTENTE"; ACCEPTEE="ACCEPTEE"; REFUSEE="REFUSEE"; ANNULEE="ANNULEE"

class User(Base):
    __tablename__="users"
    __table_args__=(CheckConstraint("(role = 'employee') = (employee_code IS NOT NULL)", name="ck_users_role_employee_code"), CheckConstraint("role = 'employee' OR email IS NOT NULL", name="ck_users_admin_email"), Index("uq_users_email_lower", func.lower(text("email")), unique=True, postgresql_where=text("email IS NOT NULL")))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    role: Mapped[Role]=mapped_column(Enum(Role, name="user_role"), nullable=False)
    employee_code: Mapped[str|None]=mapped_column(String(32), ForeignKey("employees.employee_code", ondelete="CASCADE"), unique=True)
    email: Mapped[str|None]=mapped_column(String(255), index=True)
    password_hash: Mapped[str]=mapped_column(String(255), nullable=False)
    active: Mapped[bool]=mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))
    token_version: Mapped[int]=mapped_column(Integer, default=0, nullable=False)

class Employee(Base):
    __tablename__="employees"
    __table_args__=(Index("uq_employees_code_lower", func.lower(text("employee_code")), unique=True), CheckConstraint("contract_type IN ('JOURNALIER','CDI')", name="ck_employees_contract_type"), CheckConstraint("contract_type != 'CDI' OR monthly_salary > 0", name="ck_employees_monthly_salary"),)
    employee_code: Mapped[str]=mapped_column(String(32), primary_key=True)
    first_name: Mapped[str]=mapped_column(String(100), nullable=False)
    last_name: Mapped[str]=mapped_column(String(100), nullable=False)
    email: Mapped[str|None]=mapped_column(String(255))
    phone: Mapped[str|None]=mapped_column(String(50))
    position: Mapped[str]=mapped_column(String(120), nullable=False, default="")
    hire_date: Mapped[date|None]=mapped_column(Date)
    address: Mapped[str|None]=mapped_column(Text)
    base_daily_salary: Mapped[int]=mapped_column(Integer, nullable=False, default=20000)
    contract_type: Mapped[str]=mapped_column(String(16), nullable=False, default="JOURNALIER")
    monthly_salary: Mapped[int|None]=mapped_column(Integer, nullable=True)
    active: Mapped[bool]=mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))
    workplaces: Mapped[list[Workplace]]=relationship(secondary="employee_workplaces", back_populates="employees")

class Workplace(Base):
    __tablename__="workplaces"
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str]=mapped_column(String(160), nullable=False)
    latitude: Mapped[float]=mapped_column(Numeric(10,7), nullable=False)
    longitude: Mapped[float]=mapped_column(Numeric(10,7), nullable=False)
    radius_meters: Mapped[int]=mapped_column(Integer, nullable=False, default=100)
    geofencing_enabled: Mapped[bool]=mapped_column(Boolean, nullable=False, default=True)
    employees: Mapped[list[Employee]]=relationship(secondary="employee_workplaces", back_populates="workplaces")

class EmployeeWorkplace(Base):
    __tablename__="employee_workplaces"
    employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="CASCADE"), primary_key=True)
    workplace_id: Mapped[object]=mapped_column(ForeignKey("workplaces.id", ondelete="CASCADE"), primary_key=True)

class Attendance(Base):
    __tablename__="attendance"
    __table_args__=(CheckConstraint("exit_time IS NULL OR exit_time >= entry_time", name="ck_attendance_time"), CheckConstraint("status = 'EN_COURS' OR exit_time IS NOT NULL", name="ck_attendance_finished_exit"), Index("ix_attendance_employee_entry", "employee_code", "entry_time"), Index("uq_attendance_one_open", "employee_code", unique=True, postgresql_where=text("status = 'EN_COURS'")))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="RESTRICT"), nullable=False)
    workplace_id: Mapped[object]=mapped_column(ForeignKey("workplaces.id", ondelete="SET NULL"), nullable=True)
    department_name: Mapped[str]=mapped_column(String(160), nullable=False)
    status: Mapped[AttendanceStatus]=mapped_column(Enum(AttendanceStatus, name="attendance_status"), default=AttendanceStatus.EN_COURS, nullable=False)
    entry_time: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    entry_source: Mapped[PunchSource]=mapped_column(Enum(PunchSource, name="punch_source"), default=PunchSource.EMPLOYEE)
    exit_source: Mapped[PunchSource|None]=mapped_column(Enum(PunchSource, name="punch_source", create_type=False))
    entry_location: Mapped[dict|None]=mapped_column(JSONB)
    exit_location: Mapped[dict|None]=mapped_column(JSONB)
    entry_gps_check: Mapped[dict|None]=mapped_column(JSONB)
    exit_gps_check: Mapped[dict|None]=mapped_column(JSONB)
    worked_minutes: Mapped[int]=mapped_column(Integer, default=0, nullable=False)
    daily_salary: Mapped[int]=mapped_column(Integer, default=0, nullable=False)
    site_id: Mapped[object|None]=mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))

class SalaryRecord(Base):
    __tablename__="salary_records"
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    attendance_id: Mapped[object]=mapped_column(ForeignKey("attendance.id", ondelete="RESTRICT"), unique=True)
    employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="RESTRICT"), index=True)
    date: Mapped[date]=mapped_column(Date, nullable=False)
    minutes: Mapped[int]=mapped_column(Integer, nullable=False)
    amount: Mapped[int]=mapped_column(Integer, nullable=False)
    cumulative_salary: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))

class Payment(Base):
    __tablename__="payments"
    __table_args__=(Index("ix_payments_deleted_at", "deleted_at"),)
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="RESTRICT"), index=True)
    amount: Mapped[int]=mapped_column(Integer, nullable=False)
    note: Mapped[str|None]=mapped_column(Text)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))
    created_by: Mapped[object|None]=mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[object|None]=mapped_column(UUID(as_uuid=True), nullable=True)
    deletion_reason: Mapped[str|None]=mapped_column(Text, nullable=True)

class Timesheet(Base):
    __tablename__="timesheets"
    __table_args__=(UniqueConstraint("employee_code","week_start",name="uq_timesheet_employee_week"), CheckConstraint("status IN ('DRAFT','SUBMITTED','RETURNED','APPROVED')",name="ck_timesheet_status"), Index("ix_timesheets_week_status","week_start","status"))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True),primary_key=True,default=uuid4)
    employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code",ondelete="CASCADE"),nullable=False,index=True)
    week_start: Mapped[date]=mapped_column(Date,nullable=False)
    entries: Mapped[list]=mapped_column(JSONB,nullable=False,default=list)
    status: Mapped[str]=mapped_column(String(16),nullable=False,default="DRAFT")
    submitted_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    approved_by: Mapped[object|None]=mapped_column(UUID(as_uuid=True))
    manager_note: Mapped[str|None]=mapped_column(Text)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),onupdate=lambda:datetime.now(timezone.utc))

class AppSettings(Base):
    __tablename__="settings"
    id: Mapped[int]=mapped_column(Integer, primary_key=True, default=1)
    daily_salary: Mapped[int]=mapped_column(Integer, default=20000)
    normal_work_minutes: Mapped[int]=mapped_column(Integer, default=480)
    tolerance_minutes: Mapped[int]=mapped_column(Integer, default=10)
    normal_entry_time: Mapped[str]=mapped_column(String(5), default="08:00")
    presence_check_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
    presence_check_interval_minutes: Mapped[int]=mapped_column(Integer, default=60)
    company_name: Mapped[str|None]=mapped_column(String(255))
    company_address: Mapped[str|None]=mapped_column(Text)
    company_phone: Mapped[str]=mapped_column(String(50), default="")
    company_email: Mapped[str]=mapped_column(String(255), default="")
    company_nif: Mapped[str]=mapped_column(String(80), default="")
    company_stat: Mapped[str]=mapped_column(String(80), default="")
    default_tax_rate: Mapped[float]=mapped_column(Numeric(5,2), default=0)
    document_footer: Mapped[str]=mapped_column(Text, default="")

class Client(Base):
    __tablename__="clients"
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str]=mapped_column(Text, nullable=False)
    contact_name: Mapped[str|None]=mapped_column(Text)
    phone: Mapped[str|None]=mapped_column(Text)
    email: Mapped[str|None]=mapped_column(Text)
    address: Mapped[str|None]=mapped_column(Text)
    nif: Mapped[str|None]=mapped_column(Text)
    stat: Mapped[str|None]=mapped_column(Text)
    notes: Mapped[str|None]=mapped_column(Text)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class Site(Base):
    __tablename__="sites"
    __table_args__=(Index("ix_sites_status_dates", "status", "start_date", "end_date"), Index("ix_sites_client_id", "client_id"))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str]=mapped_column(Text, nullable=False)
    client_id: Mapped[object|None]=mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    client_snapshot: Mapped[dict|None]=mapped_column(JSONB)
    workplace_id: Mapped[object|None]=mapped_column(ForeignKey("workplaces.id", ondelete="SET NULL"))
    address: Mapped[str|None]=mapped_column(Text)
    description: Mapped[str|None]=mapped_column(Text)
    start_date: Mapped[date|None]=mapped_column(Date)
    end_date: Mapped[date|None]=mapped_column(Date)
    status: Mapped[str]=mapped_column(String(20), nullable=False, default="PREPARATION")
    manual_progress: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class SiteMember(Base):
    __tablename__="site_members"
    site_id: Mapped[object]=mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
    employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="CASCADE"), primary_key=True)
    joined_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class SiteStep(Base):
    __tablename__="site_steps"
    __table_args__=(Index("ix_site_steps_site_position", "site_id", "position"),)
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_id: Mapped[object]=mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str]=mapped_column(Text, nullable=False)
    description: Mapped[str|None]=mapped_column(Text)
    position: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    due_date: Mapped[date|None]=mapped_column(Date)
    done: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
    done_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class SiteNote(Base):
    __tablename__="site_notes"
    __table_args__=(Index("ix_site_notes_site_created", "site_id", "created_at"),)
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    site_id: Mapped[object]=mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    author_name: Mapped[str]=mapped_column(Text, nullable=False)
    text: Mapped[str]=mapped_column(Text, nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class BusinessDocument(Base):
    __tablename__="documents"
    __table_args__=(Index("ix_documents_client_issue", "client_id", "issue_date"), Index("ix_documents_site", "site_id"), Index("ix_documents_type_status", "document_type", "status"))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_type: Mapped[str]=mapped_column(String(12), nullable=False)
    number: Mapped[str]=mapped_column(String(80), nullable=False, unique=True)
    status: Mapped[str]=mapped_column(String(16), nullable=False)
    issue_date: Mapped[date]=mapped_column(Date, nullable=False)
    due_date: Mapped[date|None]=mapped_column(Date)
    client_id: Mapped[object|None]=mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
    client_snapshot: Mapped[dict]=mapped_column(JSONB, nullable=False, default=dict)
    site_id: Mapped[object|None]=mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    subject: Mapped[str]=mapped_column(Text, nullable=False, default="")
    notes: Mapped[str]=mapped_column(Text, nullable=False, default="")
    paid_at: Mapped[date|None]=mapped_column(Date)
    source_document_id: Mapped[object|None]=mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
    version: Mapped[int]=mapped_column(Integer, nullable=False, default=1)
    tax_rate: Mapped[object]=mapped_column(Numeric(7,2), nullable=False, default=0)
    subtotal: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    tax_amount: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class DocumentLine(Base):
    __tablename__="document_lines"
    __table_args__=(UniqueConstraint("document_id", "position", name="uq_document_line_position"), Index("ix_document_lines_document", "document_id"))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[object]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int]=mapped_column(Integer, nullable=False)
    description: Mapped[str]=mapped_column(Text, nullable=False)
    quantity: Mapped[object]=mapped_column(Numeric(12,3), nullable=False)
    unit: Mapped[str]=mapped_column(Text, nullable=False, default="")
    unit_price: Mapped[int]=mapped_column(Integer, nullable=False)
    amount: Mapped[int]=mapped_column(Integer, nullable=False)

class DocumentVersion(Base):
    __tablename__="document_versions"
    document_id: Mapped[object]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int]=mapped_column(Integer, primary_key=True)
    snapshot: Mapped[dict]=mapped_column(JSONB, nullable=False)
    change_label: Mapped[str]=mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    changed_by_label: Mapped[str]=mapped_column(Text, nullable=False)

class DocumentCounter(Base):
    __tablename__="document_counters"
    document_type: Mapped[str]=mapped_column(String(12), primary_key=True)
    year: Mapped[int]=mapped_column(Integer, primary_key=True)
    value: Mapped[int]=mapped_column(Integer, nullable=False, default=0)

class Chat(Base):
    __tablename__="chats"
    __table_args__=(Index("uq_chats_direct_user", "direct_user_id", unique=True, postgresql_where=text("type = 'direct' AND direct_user_id IS NOT NULL")), Index("ix_chats_created", "created_at"))
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str]=mapped_column(String(10), nullable=False)
    name: Mapped[str]=mapped_column(Text, nullable=False, default="")
    announcement: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
    direct_user_id: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
    last_message_text: Mapped[str|None]=mapped_column(Text)
    last_message_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    last_message_by: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_message_name: Mapped[str|None]=mapped_column(Text)

class ChatMember(Base):
    __tablename__="chat_members"
    chat_id: Mapped[object]=mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    joined_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class ChatMessage(Base):
    __tablename__="chat_messages"
    __table_args__=(Index("ix_chat_messages_chat_created", "chat_id", "created_at"),)
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    chat_id: Mapped[object]=mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    sender_id: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    sender_name: Mapped[str]=mapped_column(Text, nullable=False)
    sender_role: Mapped[str]=mapped_column(String(16), nullable=False)
    text: Mapped[str]=mapped_column(Text, nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
    reply_to: Mapped[dict|None]=mapped_column(JSONB)

class MessageReaction(Base):
    __tablename__="message_reactions"
    message_id: Mapped[object]=mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    emoji: Mapped[str]=mapped_column(String(16), primary_key=True)

class ChatRead(Base):
    __tablename__="chat_reads"
    chat_id: Mapped[object]=mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str]=mapped_column(Text, nullable=False)
    role: Mapped[str]=mapped_column(String(16), nullable=False)
    seen_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)

class Equipment(Base):
    __tablename__="equipment"
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str]=mapped_column(Text, nullable=False)
    category: Mapped[str|None]=mapped_column(Text)
    serial_number: Mapped[str|None]=mapped_column(Text)
    description: Mapped[str|None]=mapped_column(Text)
    status: Mapped[EquipmentStatus]=mapped_column(Enum(EquipmentStatus, name="equipment_status"), nullable=False, default=EquipmentStatus.DISPONIBLE)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

class EquipmentLoan(Base):
    __tablename__="equipment_loans"
    __table_args__=(
        Index("uq_equipment_loan_one_active", "equipment_id", unique=True, postgresql_where=text("equipment_id IS NOT NULL AND status IN ('EN_COURS','RETOUR_DEMANDE')")),
        Index("uq_equipment_loan_one_pending_per_employee", "equipment_id", "employee_code", unique=True, postgresql_where=text("status = 'DEMANDE' AND equipment_id IS NOT NULL AND employee_code IS NOT NULL")),
        Index("ix_equipment_loans_status", "status"),
        Index("ix_equipment_loans_employee_requested", "employee_code", "requested_at"),
    )
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    equipment_id: Mapped[object|None]=mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
    equipment_name: Mapped[str]=mapped_column(Text, nullable=False)
    employee_code: Mapped[str|None]=mapped_column(ForeignKey("employees.employee_code", ondelete="SET NULL"))
    employee_name: Mapped[str]=mapped_column(Text, nullable=False)
    status: Mapped[LoanStatus]=mapped_column(Enum(LoanStatus, name="loan_status"), nullable=False, default=LoanStatus.DEMANDE)
    request_note: Mapped[str|None]=mapped_column(Text)
    expected_return_date: Mapped[date|None]=mapped_column(Date)
    requested_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
    decided_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    decided_by: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    refusal_reason: Mapped[str|None]=mapped_column(Text)
    loaned_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    return_requested_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    return_note: Mapped[str|None]=mapped_column(Text)
    returned_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

class RefreshToken(Base):
    __tablename__="refresh_tokens"
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str]=mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

class AuditLog(Base):
    __tablename__="audit_logs"
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc), index=True)
    user_id: Mapped[object|None]=mapped_column(UUID(as_uuid=True))
    user_label: Mapped[str|None]=mapped_column(String(255))
    action: Mapped[str]=mapped_column(String(80), nullable=False)
    target_id: Mapped[str|None]=mapped_column(String(100))
    summary: Mapped[str|None]=mapped_column(Text)

class AuthLoginAttempt(Base):
    __tablename__="auth_login_attempts"
    __table_args__=(Index("ix_auth_login_attempts_key_time","key_hash","attempted_at"),)
    id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    key_hash: Mapped[str]=mapped_column(String(64), nullable=False)
    attempted_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc), nullable=False)

def utcnow(): return datetime.now(timezone.utc)



# from __future__ import annotations
# from datetime import datetime, date, timezone
# from uuid import uuid4
# from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, CheckConstraint, Index, func, text
# from sqlalchemy.dialects.postgresql import UUID, JSONB
# from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
# import enum

# class Base(DeclarativeBase): pass
# class Role(str, enum.Enum): admin="admin"; employee="employee"
# class AttendanceStatus(str, enum.Enum): EN_COURS="EN_COURS"; TERMINEE="TERMINEE"
# class PunchSource(str, enum.Enum): EMPLOYEE="EMPLOYEE"; ADMIN="ADMIN"
# class EquipmentStatus(str, enum.Enum): DISPONIBLE="DISPONIBLE"; EMPRUNTE="EMPRUNTE"; HORS_SERVICE="HORS_SERVICE"
# class LoanStatus(str, enum.Enum): DEMANDE="DEMANDE"; REFUSEE="REFUSEE"; ANNULEE="ANNULEE"; EN_COURS="EN_COURS"; RETOUR_DEMANDE="RETOUR_DEMANDE"; RETOURNE="RETOURNE"
# class LeaveStatus(str, enum.Enum): EN_ATTENTE="EN_ATTENTE"; ACCEPTEE="ACCEPTEE"; REFUSEE="REFUSEE"; ANNULEE="ANNULEE"

# class User(Base):
#     __tablename__="users"
#     __table_args__=(CheckConstraint("(role = 'employee') = (employee_code IS NOT NULL)", name="ck_users_role_employee_code"), CheckConstraint("role = 'employee' OR email IS NOT NULL", name="ck_users_admin_email"), Index("uq_users_email_lower", func.lower(text("email")), unique=True, postgresql_where=text("email IS NOT NULL")))
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     role: Mapped[Role]=mapped_column(Enum(Role, name="user_role"), nullable=False)
#     employee_code: Mapped[str|None]=mapped_column(String(32), ForeignKey("employees.employee_code", ondelete="CASCADE"), unique=True)
#     email: Mapped[str|None]=mapped_column(String(255), index=True)
#     password_hash: Mapped[str]=mapped_column(String(255), nullable=False)
#     active: Mapped[bool]=mapped_column(Boolean, nullable=False, default=True)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))
#     token_version: Mapped[int]=mapped_column(Integer, default=0, nullable=False)

# class Employee(Base):
#     __tablename__="employees"
#     __table_args__=(Index("uq_employees_code_lower", func.lower(text("employee_code")), unique=True),)
#     employee_code: Mapped[str]=mapped_column(String(32), primary_key=True)
#     first_name: Mapped[str]=mapped_column(String(100), nullable=False)
#     last_name: Mapped[str]=mapped_column(String(100), nullable=False)
#     email: Mapped[str|None]=mapped_column(String(255))
#     phone: Mapped[str|None]=mapped_column(String(50))
#     position: Mapped[str]=mapped_column(String(120), nullable=False, default="")
#     hire_date: Mapped[date|None]=mapped_column(Date)
#     address: Mapped[str|None]=mapped_column(Text)
#     base_daily_salary: Mapped[int]=mapped_column(Integer, nullable=False, default=20000)
#     active: Mapped[bool]=mapped_column(Boolean, nullable=False, default=True)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))
#     workplaces: Mapped[list[Workplace]]=relationship(secondary="employee_workplaces", back_populates="employees")

# class Workplace(Base):
#     __tablename__="workplaces"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     name: Mapped[str]=mapped_column(String(160), nullable=False)
#     latitude: Mapped[float]=mapped_column(Numeric(10,7), nullable=False)
#     longitude: Mapped[float]=mapped_column(Numeric(10,7), nullable=False)
#     radius_meters: Mapped[int]=mapped_column(Integer, nullable=False, default=100)
#     geofencing_enabled: Mapped[bool]=mapped_column(Boolean, nullable=False, default=True)
#     employees: Mapped[list[Employee]]=relationship(secondary="employee_workplaces", back_populates="workplaces")

# class EmployeeWorkplace(Base):
#     __tablename__="employee_workplaces"
#     employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="CASCADE"), primary_key=True)
#     workplace_id: Mapped[object]=mapped_column(ForeignKey("workplaces.id", ondelete="CASCADE"), primary_key=True)

# class Attendance(Base):
#     __tablename__="attendance"
#     __table_args__=(CheckConstraint("exit_time IS NULL OR exit_time >= entry_time", name="ck_attendance_time"), CheckConstraint("status = 'EN_COURS' OR exit_time IS NOT NULL", name="ck_attendance_finished_exit"), Index("ix_attendance_employee_entry", "employee_code", "entry_time"), Index("uq_attendance_one_open", "employee_code", unique=True, postgresql_where=text("status = 'EN_COURS'")))
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="RESTRICT"), nullable=False)
#     workplace_id: Mapped[object]=mapped_column(ForeignKey("workplaces.id", ondelete="SET NULL"), nullable=True)
#     department_name: Mapped[str]=mapped_column(String(160), nullable=False)
#     status: Mapped[AttendanceStatus]=mapped_column(Enum(AttendanceStatus, name="attendance_status"), default=AttendanceStatus.EN_COURS, nullable=False)
#     entry_time: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
#     exit_time: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
#     entry_source: Mapped[PunchSource]=mapped_column(Enum(PunchSource, name="punch_source"), default=PunchSource.EMPLOYEE)
#     exit_source: Mapped[PunchSource|None]=mapped_column(Enum(PunchSource, name="punch_source", create_type=False))
#     entry_location: Mapped[dict|None]=mapped_column(JSONB)
#     exit_location: Mapped[dict|None]=mapped_column(JSONB)
#     entry_gps_check: Mapped[dict|None]=mapped_column(JSONB)
#     exit_gps_check: Mapped[dict|None]=mapped_column(JSONB)
#     worked_minutes: Mapped[int]=mapped_column(Integer, default=0, nullable=False)
#     daily_salary: Mapped[int]=mapped_column(Integer, default=0, nullable=False)
#     site_id: Mapped[object|None]=mapped_column(UUID(as_uuid=True))
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))

# class SalaryRecord(Base):
#     __tablename__="salary_records"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     attendance_id: Mapped[object]=mapped_column(ForeignKey("attendance.id", ondelete="RESTRICT"), unique=True)
#     employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="RESTRICT"), index=True)
#     date: Mapped[date]=mapped_column(Date, nullable=False)
#     minutes: Mapped[int]=mapped_column(Integer, nullable=False)
#     amount: Mapped[int]=mapped_column(Integer, nullable=False)
#     cumulative_salary: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))

# class Payment(Base):
#     __tablename__="payments"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="RESTRICT"), index=True)
#     amount: Mapped[int]=mapped_column(Integer, nullable=False)
#     note: Mapped[str|None]=mapped_column(Text)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc))
#     created_by: Mapped[object|None]=mapped_column(UUID(as_uuid=True))

# class AppSettings(Base):
#     __tablename__="settings"
#     id: Mapped[int]=mapped_column(Integer, primary_key=True, default=1)
#     daily_salary: Mapped[int]=mapped_column(Integer, default=20000)
#     normal_work_minutes: Mapped[int]=mapped_column(Integer, default=480)
#     tolerance_minutes: Mapped[int]=mapped_column(Integer, default=10)
#     normal_entry_time: Mapped[str]=mapped_column(String(5), default="08:00")
#     presence_check_enabled: Mapped[bool]=mapped_column(Boolean, default=True)
#     presence_check_interval_minutes: Mapped[int]=mapped_column(Integer, default=60)
#     company_name: Mapped[str|None]=mapped_column(String(255))
#     company_address: Mapped[str|None]=mapped_column(Text)
#     company_phone: Mapped[str]=mapped_column(String(50), default="")
#     company_email: Mapped[str]=mapped_column(String(255), default="")
#     company_nif: Mapped[str]=mapped_column(String(80), default="")
#     company_stat: Mapped[str]=mapped_column(String(80), default="")
#     default_tax_rate: Mapped[float]=mapped_column(Numeric(5,2), default=0)
#     document_footer: Mapped[str]=mapped_column(Text, default="")

# class Client(Base):
#     __tablename__="clients"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     name: Mapped[str]=mapped_column(Text, nullable=False)
#     contact_name: Mapped[str|None]=mapped_column(Text)
#     phone: Mapped[str|None]=mapped_column(Text)
#     email: Mapped[str|None]=mapped_column(Text)
#     address: Mapped[str|None]=mapped_column(Text)
#     nif: Mapped[str|None]=mapped_column(Text)
#     stat: Mapped[str|None]=mapped_column(Text)
#     notes: Mapped[str|None]=mapped_column(Text)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class Site(Base):
#     __tablename__="sites"
#     __table_args__=(Index("ix_sites_status_dates", "status", "start_date", "end_date"), Index("ix_sites_client_id", "client_id"))
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     name: Mapped[str]=mapped_column(Text, nullable=False)
#     client_id: Mapped[object|None]=mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
#     client_snapshot: Mapped[dict|None]=mapped_column(JSONB)
#     workplace_id: Mapped[object|None]=mapped_column(ForeignKey("workplaces.id", ondelete="SET NULL"))
#     address: Mapped[str|None]=mapped_column(Text)
#     description: Mapped[str|None]=mapped_column(Text)
#     start_date: Mapped[date|None]=mapped_column(Date)
#     end_date: Mapped[date|None]=mapped_column(Date)
#     status: Mapped[str]=mapped_column(String(20), nullable=False, default="PREPARATION")
#     manual_progress: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class SiteMember(Base):
#     __tablename__="site_members"
#     site_id: Mapped[object]=mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
#     employee_code: Mapped[str]=mapped_column(ForeignKey("employees.employee_code", ondelete="CASCADE"), primary_key=True)
#     joined_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class SiteStep(Base):
#     __tablename__="site_steps"
#     __table_args__=(Index("ix_site_steps_site_position", "site_id", "position"),)
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     site_id: Mapped[object]=mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
#     title: Mapped[str]=mapped_column(Text, nullable=False)
#     description: Mapped[str|None]=mapped_column(Text)
#     position: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
#     due_date: Mapped[date|None]=mapped_column(Date)
#     done: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
#     done_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

# class SiteNote(Base):
#     __tablename__="site_notes"
#     __table_args__=(Index("ix_site_notes_site_created", "site_id", "created_at"),)
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     site_id: Mapped[object]=mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
#     author_name: Mapped[str]=mapped_column(Text, nullable=False)
#     text: Mapped[str]=mapped_column(Text, nullable=False)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class BusinessDocument(Base):
#     __tablename__="documents"
#     __table_args__=(Index("ix_documents_client_issue", "client_id", "issue_date"), Index("ix_documents_site", "site_id"), Index("ix_documents_type_status", "document_type", "status"))
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     document_type: Mapped[str]=mapped_column(String(12), nullable=False)
#     number: Mapped[str]=mapped_column(String(80), nullable=False, unique=True)
#     status: Mapped[str]=mapped_column(String(16), nullable=False)
#     issue_date: Mapped[date]=mapped_column(Date, nullable=False)
#     due_date: Mapped[date|None]=mapped_column(Date)
#     client_id: Mapped[object|None]=mapped_column(ForeignKey("clients.id", ondelete="SET NULL"))
#     client_snapshot: Mapped[dict]=mapped_column(JSONB, nullable=False, default=dict)
#     site_id: Mapped[object|None]=mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
#     subject: Mapped[str]=mapped_column(Text, nullable=False, default="")
#     notes: Mapped[str]=mapped_column(Text, nullable=False, default="")
#     paid_at: Mapped[date|None]=mapped_column(Date)
#     source_document_id: Mapped[object|None]=mapped_column(ForeignKey("documents.id", ondelete="SET NULL"))
#     version: Mapped[int]=mapped_column(Integer, nullable=False, default=1)
#     tax_rate: Mapped[object]=mapped_column(Numeric(7,2), nullable=False, default=0)
#     subtotal: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
#     tax_amount: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
#     total: Mapped[int]=mapped_column(Integer, nullable=False, default=0)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
#     updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class DocumentLine(Base):
#     __tablename__="document_lines"
#     __table_args__=(UniqueConstraint("document_id", "position", name="uq_document_line_position"), Index("ix_document_lines_document", "document_id"))
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     document_id: Mapped[object]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
#     position: Mapped[int]=mapped_column(Integer, nullable=False)
#     description: Mapped[str]=mapped_column(Text, nullable=False)
#     quantity: Mapped[object]=mapped_column(Numeric(12,3), nullable=False)
#     unit: Mapped[str]=mapped_column(Text, nullable=False, default="")
#     unit_price: Mapped[int]=mapped_column(Integer, nullable=False)
#     amount: Mapped[int]=mapped_column(Integer, nullable=False)

# class DocumentVersion(Base):
#     __tablename__="document_versions"
#     document_id: Mapped[object]=mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
#     version: Mapped[int]=mapped_column(Integer, primary_key=True)
#     snapshot: Mapped[dict]=mapped_column(JSONB, nullable=False)
#     change_label: Mapped[str]=mapped_column(Text, nullable=False)
#     changed_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
#     changed_by_label: Mapped[str]=mapped_column(Text, nullable=False)

# class DocumentCounter(Base):
#     __tablename__="document_counters"
#     document_type: Mapped[str]=mapped_column(String(12), primary_key=True)
#     year: Mapped[int]=mapped_column(Integer, primary_key=True)
#     value: Mapped[int]=mapped_column(Integer, nullable=False, default=0)

# class Chat(Base):
#     __tablename__="chats"
#     __table_args__=(Index("uq_chats_direct_user", "direct_user_id", unique=True, postgresql_where=text("type = 'direct' AND direct_user_id IS NOT NULL")), Index("ix_chats_created", "created_at"))
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     type: Mapped[str]=mapped_column(String(10), nullable=False)
#     name: Mapped[str]=mapped_column(Text, nullable=False, default="")
#     announcement: Mapped[bool]=mapped_column(Boolean, nullable=False, default=False)
#     direct_user_id: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
#     last_message_text: Mapped[str|None]=mapped_column(Text)
#     last_message_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
#     last_message_by: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
#     last_message_name: Mapped[str|None]=mapped_column(Text)

# class ChatMember(Base):
#     __tablename__="chat_members"
#     chat_id: Mapped[object]=mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
#     user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
#     joined_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class ChatMessage(Base):
#     __tablename__="chat_messages"
#     __table_args__=(Index("ix_chat_messages_chat_created", "chat_id", "created_at"),)
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     chat_id: Mapped[object]=mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
#     sender_id: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
#     sender_name: Mapped[str]=mapped_column(Text, nullable=False)
#     sender_role: Mapped[str]=mapped_column(String(16), nullable=False)
#     text: Mapped[str]=mapped_column(Text, nullable=False)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
#     reply_to: Mapped[dict|None]=mapped_column(JSONB)

# class MessageReaction(Base):
#     __tablename__="message_reactions"
#     message_id: Mapped[object]=mapped_column(ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True)
#     user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
#     emoji: Mapped[str]=mapped_column(String(16), primary_key=True)

# class ChatRead(Base):
#     __tablename__="chat_reads"
#     chat_id: Mapped[object]=mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
#     user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
#     name: Mapped[str]=mapped_column(Text, nullable=False)
#     role: Mapped[str]=mapped_column(String(16), nullable=False)
#     seen_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)

# class Equipment(Base):
#     __tablename__="equipment"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     name: Mapped[str]=mapped_column(Text, nullable=False)
#     category: Mapped[str|None]=mapped_column(Text)
#     serial_number: Mapped[str|None]=mapped_column(Text)
#     description: Mapped[str|None]=mapped_column(Text)
#     status: Mapped[EquipmentStatus]=mapped_column(Enum(EquipmentStatus, name="equipment_status"), nullable=False, default=EquipmentStatus.DISPONIBLE)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))

# class EquipmentLoan(Base):
#     __tablename__="equipment_loans"
#     __table_args__=(
#         Index("uq_equipment_loan_one_active", "equipment_id", unique=True, postgresql_where=text("equipment_id IS NOT NULL AND status IN ('EN_COURS','RETOUR_DEMANDE')")),
#         Index("uq_equipment_loan_one_pending_per_employee", "equipment_id", "employee_code", unique=True, postgresql_where=text("status = 'DEMANDE' AND equipment_id IS NOT NULL AND employee_code IS NOT NULL")),
#         Index("ix_equipment_loans_status", "status"),
#         Index("ix_equipment_loans_employee_requested", "employee_code", "requested_at"),
#     )
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     equipment_id: Mapped[object|None]=mapped_column(ForeignKey("equipment.id", ondelete="SET NULL"))
#     equipment_name: Mapped[str]=mapped_column(Text, nullable=False)
#     employee_code: Mapped[str|None]=mapped_column(ForeignKey("employees.employee_code", ondelete="SET NULL"))
#     employee_name: Mapped[str]=mapped_column(Text, nullable=False)
#     status: Mapped[LoanStatus]=mapped_column(Enum(LoanStatus, name="loan_status"), nullable=False, default=LoanStatus.DEMANDE)
#     request_note: Mapped[str|None]=mapped_column(Text)
#     expected_return_date: Mapped[date|None]=mapped_column(Date)
#     requested_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False, default=lambda:datetime.now(timezone.utc))
#     decided_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
#     decided_by: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
#     refusal_reason: Mapped[str|None]=mapped_column(Text)
#     loaned_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
#     return_requested_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
#     return_note: Mapped[str|None]=mapped_column(Text)
#     returned_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
#     confirmed_by: Mapped[object|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

# class RefreshToken(Base):
#     __tablename__="refresh_tokens"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     user_id: Mapped[object]=mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
#     token_hash: Mapped[str]=mapped_column(String(64), unique=True, nullable=False)
#     expires_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), nullable=False)
#     revoked_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))

# class AuditLog(Base):
#     __tablename__="audit_logs"
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc), index=True)
#     user_id: Mapped[object|None]=mapped_column(UUID(as_uuid=True))
#     user_label: Mapped[str|None]=mapped_column(String(255))
#     action: Mapped[str]=mapped_column(String(80), nullable=False)
#     target_id: Mapped[str|None]=mapped_column(String(100))
#     summary: Mapped[str|None]=mapped_column(Text)

# class AuthLoginAttempt(Base):
#     __tablename__="auth_login_attempts"
#     __table_args__=(Index("ix_auth_login_attempts_key_time","key_hash","attempted_at"),)
#     id: Mapped[object]=mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
#     key_hash: Mapped[str]=mapped_column(String(64), nullable=False)
#     attempted_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=lambda:datetime.now(timezone.utc), nullable=False)

# def utcnow(): return datetime.now(timezone.utc)
