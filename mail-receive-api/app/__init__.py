import os
from flask import Flask
from app.db import init_app, init_db

def create_app(test_config=None):
    """Application factory for the Flask app."""
    app = Flask(__name__, instance_relative_config=True)
    
    # Default configuration
    default_db_path = os.path.join(app.instance_path, 'emails.db')
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev-secret-key-shipsy'),
        DATABASE=os.environ.get('DATABASE_PATH', default_db_path),
    )

    if test_config is not None:
        app.config.update(test_config)

    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass

    # Initialize SQLite database helpers and tables
    init_app(app)
    init_db(app)

    # Register API blueprint
    from app.routes import bp
    app.register_blueprint(bp)

    return app
