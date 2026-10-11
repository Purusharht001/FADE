"""clinician ratings and audit log

Revision ID: ef8721d5b1d9
Revises: 87e388644744
Create Date: 2026-10-11

Adds the two tables behind the validation study and the audit trail:

* clinician_ratings: one blind staging call per rater per scan.
* audit_log: who did what and when, for review sign-off, ratings, exports
  and stats attachment.

No DDL is needed for UserRole.RESEARCHER: users.role is a VARCHAR(20)
holding the member name, with no CHECK constraint.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ef8721d5b1d9'
down_revision: Union[str, Sequence[str], None] = '87e388644744'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('clinician_ratings',
    sa.Column('scan_id', sa.Uuid(), nullable=False),
    sa.Column('rater_id', sa.Uuid(), nullable=False),
    sa.Column('stage_called', sa.Enum('CN', 'MCI', 'AD', name='stage', native_enum=False, length=5), nullable=False),
    sa.Column('borderline', sa.Boolean(), nullable=False),
    sa.Column('confidence', sa.Integer(), nullable=False),
    sa.Column('notes', sa.String(length=2000), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('confidence BETWEEN 1 AND 5', name='ck_clinician_ratings_confidence'),
    sa.ForeignKeyConstraint(['rater_id'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['scan_id'], ['scans.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('scan_id', 'rater_id', name='uq_clinician_ratings_scan_rater')
    )
    op.create_index(op.f('ix_clinician_ratings_rater_id'), 'clinician_ratings', ['rater_id'], unique=False)
    op.create_index(op.f('ix_clinician_ratings_scan_id'), 'clinician_ratings', ['scan_id'], unique=False)

    op.create_table('audit_log',
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor_id', sa.Uuid(), nullable=True),
    sa.Column('actor_role', sa.String(length=20), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('entity_type', sa.String(length=32), nullable=True),
    sa.Column('entity_id', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_audit_log_actor_id'), 'audit_log', ['actor_id'], unique=False)
    op.create_index(op.f('ix_audit_log_created_at'), 'audit_log', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_log_entity_id'), 'audit_log', ['entity_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Users given the RESEARCHER role are returned to CLINICIAN, since code
    from before this revision cannot read that role.
    """
    op.drop_index(op.f('ix_audit_log_entity_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_created_at'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_actor_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index(op.f('ix_clinician_ratings_scan_id'), table_name='clinician_ratings')
    op.drop_index(op.f('ix_clinician_ratings_rater_id'), table_name='clinician_ratings')
    op.drop_table('clinician_ratings')

    users = sa.table('users', sa.column('role', sa.String()))
    op.execute(users.update().where(users.c.role == 'RESEARCHER').values(role='CLINICIAN'))
