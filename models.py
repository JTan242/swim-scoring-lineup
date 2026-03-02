# User ↔ Team (M:N), Team → Swimmer → Time ← Event.  Standard lineup/scoring schema.

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from sqlalchemy import Index

user_team_seasons = db.Table(
    "user_team_seasons",
    db.Column("user_id", db.Integer, db.ForeignKey("app_user.id"), primary_key=True),
    db.Column("team_id", db.Integer, db.ForeignKey("team.id"), primary_key=True),
    db.Column("season_year", db.Integer, primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "app_user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, pwd):
        self.password_hash = generate_password_hash(pwd)

    def check_password(self, pwd):
        return check_password_hash(self.password_hash, pwd)


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    swimmers = db.relationship("Swimmer", backref="team", lazy=True)


class Swimmer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    gender = db.Column(db.String(1))
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"))
    times = db.relationship("Time", backref="swimmer", lazy=True)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    course = db.Column(db.String(10), nullable=False)
    times = db.relationship("Time", backref="event", lazy=True)


class Time(db.Model):
    __tablename__ = "time"
    __table_args__ = (
        Index("ix_time_event_time", "event_id", "time_secs"),
        Index("ix_time_season", "season_year"),
        Index("ix_time_swimmer", "swimmer_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    swimmer_id = db.Column(db.Integer, db.ForeignKey("swimmer.id"), index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), index=True)
    time_secs = db.Column(db.Numeric, nullable=False)
    meet = db.Column(db.String(200))
    date = db.Column(db.Date)
    season_year = db.Column(db.Integer, index=True)
