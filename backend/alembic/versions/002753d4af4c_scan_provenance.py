"""scan provenance columns and honest phantom labelling

Revision ID: 002753d4af4c
Revises: d65864356d55
Create Date: 2026-10-11

Adds the provenance columns real data needs on `scans`, and relabels the
rows that already exist.

No DDL is needed for the new enum members (DataSource.PHANTOM,
ScanStatus.AWAITING_VOLUMETRY / SEGMENTING): every enum column in this
schema is a plain VARCHAR holding the member NAME, with no CHECK
constraint, and each column is already wide enough.

Data changes, both of which follow from one fact: before this revision
the only extraction path that existed was the in-house phantom volumetry,
which rejects any volume that is not the phantom's 64x64x64 grid.

* Every existing scan is stamped extraction_method = INHOUSE_PHANTOM.
* Every patient with a COMPLETED scan is relabelled source = PHANTOM. A
  completed scan could only have come from the phantom, yet the seed
  stamped those patients OASIS / ADNI / CLINIC (assumption C1).

The downgrade drops the columns but does not restore the old patient
labels: they were wrong, and they are not recorded anywhere to restore.
Code from before this revision cannot read a PHANTOM row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '002753d4af4c'
down_revision: Union[str, Sequence[str], None] = 'd65864356d55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXTRACTION_METHOD = sa.Enum(
    'INHOUSE_PHANTOM', 'FREESURFER_STATS', 'FASTSURFER', 'MANUAL_BIOMARKERS',
    name='extractionmethod', native_enum=False, length=30,
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('scans', sa.Column('extraction_method', _EXTRACTION_METHOD, nullable=True))
    op.add_column('scans', sa.Column('external_subject_id', sa.String(length=64), nullable=True))
    op.add_column('scans', sa.Column('raw_volumes', sa.JSON(), nullable=True))
    op.add_column('scans', sa.Column('qc_flags', sa.JSON(), nullable=True))
    op.add_column('scans', sa.Column('calibration_version', sa.String(length=64), nullable=True))
    op.add_column('scans', sa.Column('engine_version', sa.String(length=32), nullable=True))
    op.add_column('scans', sa.Column('reference_label', sa.Float(), nullable=True))
    op.create_index(
        op.f('ix_scans_external_subject_id'), 'scans', ['external_subject_id'], unique=False
    )

    scans = sa.table(
        'scans',
        sa.column('patient_id', sa.Uuid()),
        sa.column('status', sa.String()),
        sa.column('extraction_method', sa.String()),
        sa.column('calibration_version', sa.String()),
        sa.column('engine_version', sa.String()),
    )
    patients = sa.table(
        'patients',
        sa.column('id', sa.Uuid()),
        sa.column('source', sa.String()),
    )

    op.execute(scans.update().values(extraction_method='INHOUSE_PHANTOM'))
    op.execute(
        scans.update()
        .where(scans.c.status == 'COMPLETED')
        .values(calibration_version='phantom.v0', engine_version='1.0.0')
    )
    op.execute(
        patients.update()
        .where(
            patients.c.id.in_(
                sa.select(scans.c.patient_id).where(scans.c.status == 'COMPLETED')
            )
        )
        .values(source='PHANTOM')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_scans_external_subject_id'), table_name='scans')
    # Batch mode: SQLite cannot drop a column in place on older versions.
    with op.batch_alter_table('scans') as batch_op:
        batch_op.drop_column('reference_label')
        batch_op.drop_column('engine_version')
        batch_op.drop_column('calibration_version')
        batch_op.drop_column('qc_flags')
        batch_op.drop_column('raw_volumes')
        batch_op.drop_column('external_subject_id')
        batch_op.drop_column('extraction_method')
