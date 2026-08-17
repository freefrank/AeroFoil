"""add title request users relation and denied status support

Revision ID: b1c2d3e4f5a6
Revises: 9f3a12c7b4d2

"""

from alembic import op
import sqlalchemy as sa
import datetime


# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5a6'
down_revision = '9f3a12c7b4d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'title_request_users',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_id', sa.Integer(), sa.ForeignKey('title_requests.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'request_id', name='uq_title_request_users_user_request'),
    )
    op.create_index('ix_title_request_users_user_id', 'title_request_users', ['user_id'])
    op.create_index('ix_title_request_users_request_id', 'title_request_users', ['request_id'])
    op.create_index('ix_title_request_users_created_at', 'title_request_users', ['created_at'])

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, user_id, created_at FROM title_requests")).fetchall()
    for row in rows:
        if row.user_id is None:
            continue
        bind.execute(
            sa.text(
                "INSERT OR IGNORE INTO title_request_users (user_id, request_id, created_at) VALUES (:user_id, :request_id, :created_at)"
            ),
            {
                'user_id': int(row.user_id),
                'request_id': int(row.id),
                'created_at': row.created_at or datetime.datetime.utcnow(),
            },
        )


def downgrade():
    op.drop_index('ix_title_request_users_created_at', table_name='title_request_users')
    op.drop_index('ix_title_request_users_request_id', table_name='title_request_users')
    op.drop_index('ix_title_request_users_user_id', table_name='title_request_users')
    op.drop_table('title_request_users')
