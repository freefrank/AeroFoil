"""Add ignored-content flag to apps.

Revision ID: c6f8a2d9e1b4
Revises: 9f3a12c7b4d2
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = 'c6f8a2d9e1b4'
down_revision = '9f3a12c7b4d2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('apps') as batch_op:
        batch_op.add_column(sa.Column('ignored', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('apps') as batch_op:
        batch_op.drop_column('ignored')
