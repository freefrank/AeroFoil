"""Merge heads 7b2a4d9e1f2c and b1c2d3e4f5a6

Revision ID: 48e539769ef3
Revises: 7b2a4d9e1f2c, b1c2d3e4f5a6

This merge reconciles:
- 7b2a4d9e1f2c (Added client UID/login fields to user table)
- b1c2d3e4f5a6 (Added title request users relation table)

"""

from alembic import op


# revision identifiers, used by Alembic.
revision = '48e539769ef3'
down_revision = ('7b2a4d9e1f2c', 'b1c2d3e4f5a6')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
