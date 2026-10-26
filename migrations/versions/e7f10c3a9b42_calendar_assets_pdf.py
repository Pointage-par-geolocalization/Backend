"""Add calendar rules, durable site attachments, and business logo storage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision='e7f10c3a9b42'
down_revision='d4e9a2f1c8b7'
branch_labels=None
depends_on=None
def upgrade():
 op.create_table('calendar_holidays',sa.Column('id',postgresql.UUID(as_uuid=True),primary_key=True),sa.Column('day',sa.Date(),nullable=False),sa.Column('name',sa.String(160),nullable=False),sa.Column('working',sa.Boolean(),nullable=False,server_default=sa.false()))
 op.create_index('ix_calendar_holidays_day','calendar_holidays',['day'])
 op.create_table('calendar_settings',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('working_rules',postgresql.JSONB(),nullable=True),sa.Column('hours_rules',postgresql.JSONB(),nullable=True))
 op.create_table('site_attachments',sa.Column('id',postgresql.UUID(as_uuid=True),primary_key=True),sa.Column('site_id',postgresql.UUID(as_uuid=True),sa.ForeignKey('sites.id',ondelete='CASCADE'),nullable=False),sa.Column('filename',sa.String(255),nullable=False),sa.Column('attachment_type',sa.String(50),nullable=False,server_default='Autre'),sa.Column('content_type',sa.String(120),nullable=False),sa.Column('size',sa.Integer(),nullable=False),sa.Column('data',postgresql.BYTEA(),nullable=False),sa.Column('uploaded_by',postgresql.UUID(as_uuid=True)),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),sa.Column('deleted_at',sa.DateTime(timezone=True)))
 op.create_index('ix_site_attachments_site_id','site_attachments',['site_id'])
 op.create_table('business_logo',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('content_type',sa.String(40),nullable=False),sa.Column('data',postgresql.BYTEA(),nullable=False),sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()))
def downgrade():
 op.drop_table('business_logo');op.drop_index('ix_site_attachments_site_id',table_name='site_attachments');op.drop_table('site_attachments');op.drop_table('calendar_settings');op.drop_index('ix_calendar_holidays_day',table_name='calendar_holidays');op.drop_table('calendar_holidays')
