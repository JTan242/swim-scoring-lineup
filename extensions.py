# Extensions live here so models/routes can import without circular deps.

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_caching import Cache

db = SQLAlchemy()

login_manager = LoginManager()
login_manager.login_view = "main.login"

cache = Cache()
