# Forms for login, register, scrape (import), and dashboard selection.

from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, SelectField,
    IntegerField, widgets, SelectMultipleField,
)
from wtforms.validators import (
    DataRequired, NumberRange, Length, EqualTo, ValidationError,
)


def _strong_password(form, field):
    """8+ chars, at least one upper, one lower, one digit."""
    pw = field.data or ""
    errors = []
    if len(pw) < 8:
        errors.append("at least 8 characters")
    if not any(c.isupper() for c in pw):
        errors.append("one uppercase letter")
    if not any(c.islower() for c in pw):
        errors.append("one lowercase letter")
    if not any(c.isdigit() for c in pw):
        errors.append("one digit")
    if errors:
        raise ValidationError("Password must contain " + ", ".join(errors) + ".")


class MultiCheckboxField(SelectMultipleField):
    """Team/season choices as checkboxes instead of a dropdown."""
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])


class RegistrationForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[DataRequired(), Length(min=3, max=80)],
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(), _strong_password],
    )
    confirm = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )


class ScrapeForm(FlaskForm):
    team_name = StringField("Team Name", validators=[DataRequired()])
    gender = SelectField(
        "Gender",
        choices=[("M", "Male"), ("F", "Female")],
        validators=[DataRequired()],
    )
    year = IntegerField(
        "Season Year (e.g. 2025 = 2024-2025)",
        validators=[DataRequired(), NumberRange(min=1997, max=2100)],
    )


class SelectionForm(FlaskForm):
    teams = MultiCheckboxField("Team & Season", coerce=str)
    event = SelectField("Event", coerce=str, validators=[DataRequired()])
    top_n = IntegerField(
        "Number of places",
        default=8,
        validators=[DataRequired(), NumberRange(min=1, max=16)],
    )
    scoring_mode = SelectField(
        "Relay Scoring Mode",
        choices=[("unscored", "Non-Scoring"), ("scored", "Scoring")],
        default="unscored",
    )
