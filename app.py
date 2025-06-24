from dotenv import load_dotenv
load_dotenv()  
from flask import Flask
from config import Config
from extensions import db, login_manager
from routes   import main as main_bp

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    login_manager.init_app(app)
    app.register_blueprint(main_bp)
    return app

if __name__ == '__main__':
    create_app().run(debug=True, port=5001)
