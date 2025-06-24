from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db

user_teams = db.Table(
    'user_teams',
    db.Column('user_id',   db.Integer, db.ForeignKey('user.id')),
    db.Column('team_id',   db.Integer, db.ForeignKey('team.id'))
)

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    teams         = db.relationship(
        'Team',
        secondary=user_teams,
        backref=db.backref('users', lazy='dynamic')
    )
    def set_password(self, pwd):
        self.password_hash = generate_password_hash(pwd)
    def check_password(self, pwd):
        return check_password_hash(self.password_hash, pwd)

class Team(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), unique=True, nullable=False)
    swimmers = db.relationship('Swimmer', backref='team', lazy=True)

class Swimmer(db.Model):
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(100), nullable=False)
    gender   = db.Column(db.String(1))
    team_id  = db.Column(db.Integer, db.ForeignKey('team.id'))
    times    = db.relationship('Time', backref='swimmer', lazy=True)

class Event(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    name    = db.Column(db.String(100), nullable=False)
    course  = db.Column(db.String(10), nullable=False)
    times   = db.relationship('Time', backref='event', lazy=True)

class Time(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    swimmer_id  = db.Column(db.Integer, db.ForeignKey('swimmer.id'))
    event_id    = db.Column(db.Integer, db.ForeignKey('event.id'))
    time_secs   = db.Column(db.Numeric)
    meet        = db.Column(db.String(200))
    date        = db.Column(db.Date)
    season_year = db.Column(db.Integer, nullable=False, index=True)

