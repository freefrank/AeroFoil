"""Add per-user cheat catalogue access.

Revision ID: d8e7f6a5b4c3
Revises: c6f8a2d9e1b4, 48e539769ef3
"""

from alembic import op
import sqlalchemy as sa


revision = 'd8e7f6a5b4c3'
down_revision = ('c6f8a2d9e1b4', '48e539769ef3')
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user') as batch_op:
        batch_op.add_column(sa.Column('cheat_access', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('user') as batch_op:
        batch_op.drop_column('cheat_access')
