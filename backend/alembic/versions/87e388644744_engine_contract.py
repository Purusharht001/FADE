"""engine contract: indeterminate results, severity, calibration version

Revision ID: 87e388644744
Revises: 002753d4af4c
Create Date: 2026-10-11

Schema:

* fis_results gains determinate, indeterminate_reason, severity and
  calibration_version, and `stage` becomes nullable: an indeterminate
  result has no stage.
* biomarker_readings.abnormality becomes nullable: a value the active
  calibration does not apply to has no abnormality score.

Data: existing results were all produced with the phantom calibration, so
they are stamped calibration_version = 'phantom.v0'. Results in which no
rule fired were stored with stage CN by the old tie-break (assumption C4);
they are corrected to indeterminate.

Uses batch mode throughout so the column alterations work on SQLite, which
cannot alter a column in place. On PostgreSQL batch mode emits plain
ALTER TABLE statements.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '87e388644744'
down_revision: Union[str, Sequence[str], None] = '002753d4af4c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STAGE = sa.Enum('CN', 'MCI', 'AD', name='stage', native_enum=False, length=5)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('fis_results') as batch_op:
        batch_op.add_column(
            sa.Column('determinate', sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column('indeterminate_reason', sa.String(length=500), nullable=True)
        )
        batch_op.add_column(sa.Column('severity', sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'calibration_version', sa.String(length=64), nullable=False,
                server_default='unknown',
            )
        )
        batch_op.alter_column('stage', existing_type=_STAGE, nullable=True)

    with op.batch_alter_table('biomarker_readings') as batch_op:
        batch_op.alter_column('abnormality', existing_type=sa.Float(), nullable=True)

    fis_results = sa.table(
        'fis_results',
        sa.column('id', sa.Uuid()),
        sa.column('stage', sa.String()),
        sa.column('confidence', sa.Float()),
        sa.column('uncertainty', sa.Float()),
        sa.column('needs_review', sa.Boolean()),
        sa.column('membership', sa.JSON()),
        sa.column('fired_rules', sa.JSON()),
        sa.column('determinate', sa.Boolean()),
        sa.column('indeterminate_reason', sa.String()),
        sa.column('calibration_version', sa.String()),
    )
    op.execute(fis_results.update().values(calibration_version='phantom.v0'))

    # C4: a result with no fired rule was never a CN finding. JSON equality
    # is not portable across SQLite and PostgreSQL, so select and compare
    # in Python; the table holds one row per scan. `--sql` (offline) mode
    # has no connection to read from, so this correction is skipped there.
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    rows = bind.execute(sa.select(fis_results.c.id, fis_results.c.fired_rules)).fetchall()
    no_rule_ids = [row.id for row in rows if not row.fired_rules]
    for result_id in no_rule_ids:
        bind.execute(
            fis_results.update()
            .where(fis_results.c.id == result_id)
            .values(
                stage=None,
                determinate=False,
                indeterminate_reason='no_rule_fired',
                needs_review=True,
                confidence=0.0,
                uncertainty=100.0,
                membership={'CN': 0.0, 'MCI': 0.0, 'AD': 0.0},
            )
        )


def downgrade() -> None:
    """Downgrade schema.

    Indeterminate results and unscored readings cannot be represented in
    the old schema, where `stage` and `abnormality` are NOT NULL. Those
    rows are deleted (the scans stay) rather than given an invented stage
    or an invented "normal" score.
    """
    fis_results = sa.table('fis_results', sa.column('stage', sa.String()))
    biomarker_readings = sa.table('biomarker_readings', sa.column('abnormality', sa.Float()))
    op.execute(fis_results.delete().where(fis_results.c.stage.is_(None)))
    op.execute(biomarker_readings.delete().where(biomarker_readings.c.abnormality.is_(None)))

    with op.batch_alter_table('biomarker_readings') as batch_op:
        batch_op.alter_column('abnormality', existing_type=sa.Float(), nullable=False)

    with op.batch_alter_table('fis_results') as batch_op:
        batch_op.alter_column('stage', existing_type=_STAGE, nullable=False)
        batch_op.drop_column('calibration_version')
        batch_op.drop_column('severity')
        batch_op.drop_column('indeterminate_reason')
        batch_op.drop_column('determinate')
