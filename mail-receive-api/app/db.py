import os
import sqlite3
from flask import current_app, g

def get_db():
    """Opens a new database connection if there is none yet for the current application context."""
    if 'db' not in g:
        db_path = current_app.config['DATABASE']
        # Ensure parent directory exists
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        g.db = sqlite3.connect(
            db_path,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db(app=None):
    """Initialize database schema from schema.sql and perform lightweight migrations."""
    def _run_init(database):
        schema_path = os.path.join(os.path.dirname(__file__), 'schema.sql')
        with open(schema_path, 'r', encoding='utf-8') as f:
            database.executescript(f.read())
        try:
            database.execute("ALTER TABLE emails ADD COLUMN audit_data TEXT DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass
        database.commit()

    if app:
        with app.app_context():
            _run_init(get_db())
    else:
        _run_init(get_db())


def init_app(app):
    """Register database functions with the Flask app."""
    app.teardown_appcontext(close_db)
