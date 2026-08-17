import logging

from app.db import db, TitleRequests, TitleRequestUsers, Titles, is_migration_needed, create_db_backup, get_alembic_cfg, command
from sqlalchemy.orm import joinedload
from sqlalchemy import inspect


logger = logging.getLogger('main')

_title_request_users_available = None
_title_request_users_upgrade_attempted = False


def _maybe_auto_upgrade_for_title_request_users():
    global _title_request_users_upgrade_attempted
    if _title_request_users_upgrade_attempted:
        return
    _title_request_users_upgrade_attempted = True
    try:
        if is_migration_needed():
            create_db_backup()
            command.upgrade(get_alembic_cfg(), "head")
            logger.info("Applied automatic DB migration after missing title_request_users detection.")
    except Exception as exc:
        logger.warning("Automatic DB migration attempt failed: %s", exc)


def _has_title_request_users_table():
    global _title_request_users_available
    if _title_request_users_available is not None:
        return bool(_title_request_users_available)
    try:
        inspector = inspect(db.engine)
        _title_request_users_available = bool(inspector.has_table('title_request_users'))
        if not _title_request_users_available:
            _maybe_auto_upgrade_for_title_request_users()
            inspector = inspect(db.engine)
            _title_request_users_available = bool(inspector.has_table('title_request_users'))
    except Exception:
        _title_request_users_available = False
    return bool(_title_request_users_available)


def create_title_request(user_id, title_id, title_name=None):
    title_id = (title_id or '').strip().upper()
    title_name = (title_name or '').strip() or None
    if not title_id:
        return False, 'Missing title_id.', None

    if Titles.query.filter_by(title_id=title_id).first() is not None:
        return False, 'Title is already in the library.', None

    if not _has_title_request_users_table():
        existing = TitleRequests.query.filter_by(user_id=user_id, title_id=title_id, status='open').first()
        if existing is not None:
            return True, 'Request already exists.', existing
        req = TitleRequests(user_id=user_id, title_id=title_id, title_name=title_name, status='open')
        db.session.add(req)
        db.session.commit()
        return True, 'Request created.', req

    existing = TitleRequests.query.filter_by(title_id=title_id).order_by(TitleRequests.created_at.desc()).first()
    if existing is not None:
        if not TitleRequestUsers.query.filter_by(user_id=user_id, request_id=existing.id).first():
            db.session.add(TitleRequestUsers(user_id=user_id, request_id=existing.id))
            db.session.commit()
        return True, 'Request already exists.', existing

    req = TitleRequests(user_id=user_id, title_id=title_id, title_name=title_name, status='open')
    db.session.add(req)
    db.session.flush()
    db.session.add(TitleRequestUsers(user_id=user_id, request_id=req.id))
    db.session.commit()
    return True, 'Request created.', req


def list_requests(user_id=None, include_all=False, limit=500):
    q = TitleRequests.query
    has_relation_table = _has_title_request_users_table()
    if not include_all:
        if has_relation_table:
            q = (
                q.join(TitleRequestUsers, TitleRequestUsers.request_id == TitleRequests.id)
                .filter(TitleRequestUsers.user_id == user_id)
            )
        else:
            q = q.filter_by(user_id=user_id)
    else:
        q = q.options(joinedload(TitleRequests.user))
        if has_relation_table:
            q = q.options(joinedload(TitleRequests.request_users).joinedload(TitleRequestUsers.user))
    return q.order_by(TitleRequests.created_at.desc()).limit(limit).all()
